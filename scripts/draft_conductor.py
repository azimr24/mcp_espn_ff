#!/usr/bin/env python3
"""Run one compact ESPN draft monitor with guarded pick commands."""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import random
import select
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import espn_fantasy_server as server  # noqa: E402


def _completed_pick(pick: dict[str, Any]) -> bool:
    player_id = pick.get("playerId")
    return isinstance(player_id, int) and player_id > 0


class DraftTracker:
    """Merge REST snapshots and draft-room events into one small state object."""

    def __init__(
        self,
        league_id: int,
        year: int,
        team_id: int,
        scheduled_picks: list[dict[str, Any]],
        player_map: dict[Any, Any],
    ) -> None:
        self.league_id = league_id
        self.year = year
        self.team_id = team_id
        self.scheduled = sorted(
            [dict(pick) for pick in scheduled_picks],
            key=lambda pick: pick.get("overallPickNumber") or 0,
        )
        self.player_map = player_map
        self.completed: dict[int, dict[str, Any]] = {}
        self.current_team_id: int | None = None
        self.current_team_source: str | None = None
        self.time_to_pick_ms: int | None = None
        self.in_progress = False
        self.drafted = False
        self.draft_room_state: int | None = None
        self.lock = threading.RLock()

    def restore(self, saved: dict[str, Any]) -> None:
        if (
            saved.get("league_id") != self.league_id
            or saved.get("year") != self.year
            or saved.get("team_id") != self.team_id
        ):
            return
        with self.lock:
            for pick in saved.get("completed_picks", []):
                overall = pick.get("overall_pick")
                player_id = pick.get("player_id")
                if isinstance(overall, int) and isinstance(player_id, int):
                    self.completed[overall] = {
                        "overall_pick": overall,
                        "team_id": pick.get("team_id"),
                        "player_id": player_id,
                        "player_name": pick.get("player_name"),
                        "slot_id": pick.get("slot_id"),
                        "source": "recovery",
                    }

    def merge_rest(self, draft: dict[str, Any]) -> list[dict[str, Any]]:
        new_picks: list[dict[str, Any]] = []
        with self.lock:
            self.in_progress = bool(draft.get("inProgress", False))
            self.drafted = bool(draft.get("drafted", False))
            rest_picks = sorted(
                draft.get("picks", []),
                key=lambda pick: pick.get("overallPickNumber") or 0,
            )
            if rest_picks:
                self.scheduled = [dict(pick) for pick in rest_picks]
            for pick in rest_picks:
                if not _completed_pick(pick):
                    continue
                overall = pick.get("overallPickNumber")
                if not isinstance(overall, int) or overall in self.completed:
                    continue
                record = self._record_from_rest(pick)
                self.completed[overall] = record
                new_picks.append(record)
            if self.current_team_source != "stream":
                next_pick = self._next_scheduled_locked()
                self.current_team_id = next_pick.get("teamId") if next_pick else None
                self.current_team_source = "rest" if next_pick else None
                self.time_to_pick_ms = None
        return new_picks

    def apply_message(self, message: str) -> dict[str, Any] | None:
        fields = message.strip().split(" ")
        if not fields or not fields[0]:
            return None
        command = fields[0]
        with self.lock:
            if command == "SELECTING" and len(fields) >= 3:
                self.current_team_id = int(fields[1])
                self.current_team_source = "stream"
                self.time_to_pick_ms = int(fields[2])
                return {"type": "SELECTING"}
            if command == "SELECTED" and len(fields) >= 4:
                next_pick = self._next_scheduled_locked()
                if next_pick is None:
                    return None
                overall = int(next_pick.get("overallPickNumber"))
                player_id = int(fields[2])
                record = {
                    "overall_pick": overall,
                    "round": next_pick.get("roundId"),
                    "round_pick": next_pick.get("roundPickNumber"),
                    "team_id": int(fields[1]),
                    "player_id": player_id,
                    "player_name": self.player_map.get(player_id),
                    "slot_id": int(fields[3]),
                    "source": "draft_stream",
                }
                self.completed[overall] = record
                self.current_team_id = None
                self.current_team_source = None
                self.time_to_pick_ms = None
                return {"type": "SELECTED", "pick": record}
            if command == "STATE" and len(fields) >= 2:
                self.draft_room_state = int(fields[1])
                self.in_progress = self.draft_room_state == 1
                self.drafted = self.draft_room_state == 2
                return {"type": "STATE"}
            if command == "UNDONE" and len(fields) >= 2:
                overall = int(fields[1])
                removed = self.completed.pop(overall, None)
                self.current_team_id = None
                self.current_team_source = None
                self.time_to_pick_ms = None
                return {"type": "UNDONE", "pick": removed}
            if command == "RESET":
                self.completed.clear()
                self.current_team_id = None
                self.current_team_source = None
                self.time_to_pick_ms = None
                return {"type": "RESET"}
            if command == "ERROR":
                return {"type": "ERROR", "message": "ESPN rejected a draft command."}
        return None

    def effective_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(draft)
        with self.lock:
            for pick in result.get("picks", []):
                overall = pick.get("overallPickNumber")
                completed = self.completed.get(overall)
                if completed:
                    pick["playerId"] = completed["player_id"]
                    pick["teamId"] = completed["team_id"]
            if self.draft_room_state is not None:
                result["inProgress"] = self.draft_room_state == 1
                result["drafted"] = self.draft_room_state == 2
        return result

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            completed = [self.completed[key] for key in sorted(self.completed)]
            next_pick = self._next_scheduled_locked()
            current_team = self.current_team_id
            if current_team is None and next_pick:
                current_team = next_pick.get("teamId")
            return {
                "league_id": self.league_id,
                "year": self.year,
                "team_id": self.team_id,
                "drafted": self.drafted,
                "in_progress": self.in_progress,
                "draft_room_state": self.draft_room_state,
                "completed_pick_count": len(completed),
                "scheduled_pick_count": len(self.scheduled),
                "current_team_id": current_team,
                "time_to_pick_ms": self.time_to_pick_ms,
                "next_pick": self._scheduled_summary(next_pick),
                "my_picks": [
                    pick for pick in completed if pick.get("team_id") == self.team_id
                ],
                "recent_picks": completed[-8:],
                "completed_picks": completed,
            }

    def complete(self) -> bool:
        with self.lock:
            return bool(self.scheduled) and len(self.completed) >= len(self.scheduled)

    def _next_scheduled_locked(self) -> dict[str, Any] | None:
        for pick in self.scheduled:
            if pick.get("overallPickNumber") not in self.completed:
                return pick
        return None

    def _record_from_rest(self, pick: dict[str, Any]) -> dict[str, Any]:
        player_id = pick.get("playerId")
        return {
            "overall_pick": pick.get("overallPickNumber"),
            "round": pick.get("roundId"),
            "round_pick": pick.get("roundPickNumber"),
            "team_id": pick.get("teamId"),
            "player_id": player_id,
            "player_name": self.player_map.get(player_id),
            "slot_id": pick.get("slotId"),
            "source": "rest",
        }

    @staticmethod
    def _scheduled_summary(pick: dict[str, Any] | None) -> dict[str, Any] | None:
        if pick is None:
            return None
        return {
            "overall_pick": pick.get("overallPickNumber"),
            "round": pick.get("roundId"),
            "round_pick": pick.get("roundPickNumber"),
            "team_id": pick.get("teamId"),
        }


class DraftConductor:
    """Keep a draft connection active and accept compact JSON commands."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.api = server.ESPNFantasyFootballAPI(
            cache_ttl_seconds=300,
            draft_cache_ttl_seconds=0,
        )
        self.session_id = "draft-conductor"
        self.stop_event = threading.Event()
        self.output_lock = threading.Lock()
        self.persistence_lock = threading.Lock()
        self.last_stream_status: str | None = None
        self.last_signature: str | None = None
        self.was_on_clock = False
        self.threads: list[threading.Thread] = []

        league, draft = self.api.get_draft(
            self.session_id,
            args.league_id,
            args.year,
            True,
        )
        self.league = league
        self.team = server._team_by_id(league, args.team_id)
        self.tracker = DraftTracker(
            args.league_id,
            args.year,
            args.team_id,
            draft.get("picks", []),
            league.player_map,
        )
        self.state_path = args.state_dir / (
            f"{args.league_id}-{args.year}-team-{args.team_id}.json"
        )
        self.ledger_path = args.state_dir / (
            f"{args.league_id}-{args.year}-team-{args.team_id}.jsonl"
        )
        self._restore()
        new_picks = self.tracker.merge_rest(draft)
        for pick in new_picks:
            self._append_ledger("SELECTED", pick)
        self._publish_state(force=True)
        self._emit_ready()

    def run(self) -> int:
        if self.args.once:
            return 0
        self.threads = [
            threading.Thread(target=self._rest_loop, name="draft-rest", daemon=True),
        ]
        if not self.args.no_stream:
            self.threads.append(
                threading.Thread(
                    target=self._stream_loop,
                    name="draft-stream",
                    daemon=True,
                )
            )
        for thread in self.threads:
            thread.start()
        self._command_loop()
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self._emit("stopped")
        return 0

    def _rest_loop(self) -> None:
        while not self.stop_event.wait(self.args.poll_interval):
            try:
                self.league, draft = self.api.get_draft(
                    self.session_id,
                    self.args.league_id,
                    self.args.year,
                    True,
                )
                new_picks = self.tracker.merge_rest(draft)
                for pick in new_picks:
                    self._append_ledger("SELECTED", pick)
                self._publish_state()
                if self.tracker.complete():
                    self.stop_event.set()
            except Exception as error:
                self._emit("rest_error", error=self._safe_error(error))

    def _stream_loop(self) -> None:
        while not self.stop_event.is_set():
            response = None
            try:
                self.league = self.api.get_league(
                    self.session_id,
                    self.args.league_id,
                    self.args.year,
                )
                token = server._draft_security_token(self.league, self.args.team_id)
                request = self.league.espn_request
                url = (
                    "https://fantasydraft.espn.com/game-ffl/"
                    f"league-{self.args.league_id}/sse/JOIN"
                )
                params = {
                    "1": server.DRAFT_GAME_ID,
                    "2": self.args.league_id,
                    "3": self.args.team_id,
                    "4": request.cookies["SWID"],
                    "5": token,
                    "6": "false",
                    "7": "false",
                    "8": "KONA",
                    "nocache": random.randrange(1_000_000),
                }
                response = requests.get(
                    url,
                    params=params,
                    headers={
                        "Accept": "text/event-stream",
                        "Origin": "https://fantasy.espn.com",
                        "Referer": "https://fantasy.espn.com/",
                        "User-Agent": "Mozilla/5.0",
                    },
                    cookies=request.cookies,
                    stream=True,
                    timeout=(3.05, 30),
                )
                if response.status_code != 200:
                    self._stream_status(f"http_{response.status_code}")
                    self.stop_event.wait(self.args.reconnect_interval)
                    continue
                self._stream_status("connected")
                for raw_line in response.iter_lines(decode_unicode=True):
                    if self.stop_event.is_set():
                        break
                    line = raw_line or ""
                    if not line.startswith("data:"):
                        continue
                    message = line[5:].strip()
                    event = self.tracker.apply_message(message)
                    if event is None:
                        continue
                    pick = event.get("pick")
                    if event["type"] in {"SELECTED", "UNDONE", "RESET"}:
                        self._append_ledger(event["type"], pick)
                    if event["type"] == "ERROR":
                        self._emit("draft_error", error=event["message"])
                    self._publish_state(force=event["type"] != "SELECTING")
            except Exception as error:
                self._stream_status(f"error_{type(error).__name__}")
            finally:
                if response is not None:
                    response.close()
            self.stop_event.wait(self.args.reconnect_interval)

    def _command_loop(self) -> None:
        while not self.stop_event.is_set():
            readable, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not readable:
                continue
            line = sys.stdin.readline()
            if not line:
                self.stop_event.set()
                break
            try:
                command = json.loads(line)
                self._handle_command(command)
            except Exception as error:
                self._emit("command_error", error=self._safe_error(error))

    def _handle_command(self, command: dict[str, Any]) -> None:
        name = command.get("command")
        request_id = command.get("request_id")
        if name == "state":
            self._emit(
                "command_result",
                request_id=request_id,
                state=self._compact_state(),
            )
            return
        if name == "pool":
            limit = int(command.get("limit", 15))
            position = command.get("position")
            if position:
                position = str(position).upper()
            server._validate_draft_query(limit, 0, position)
            players = server._get_draft_pool_data(
                self.league,
                self.args.year,
                limit,
                0,
                position,
            )
            self._emit(
                "command_result",
                request_id=request_id,
                command="pool",
                players=players,
            )
            return
        if name == "preview_pick":
            player_id = int(command["player_id"])
            action, preview = self._prepare_pick(player_id)
            token = self.api.create_write_confirmation(self.session_id, action)
            self._emit(
                "command_result",
                request_id=request_id,
                command="preview_pick",
                preview=preview,
                confirmation_token=token,
                expires_in_seconds=server.WRITE_CONFIRMATION_TTL_SECONDS,
                writes_enabled=server._write_enabled(),
                draft_writes_enabled=server._draft_write_enabled(),
            )
            return
        if name == "submit_pick":
            if not server._write_enabled() or not server._draft_write_enabled():
                raise PermissionError("The draft write flags are not enabled.")
            player_id = int(command["player_id"])
            confirmation_token = str(command["confirmation_token"])
            action, preview = self._prepare_pick(player_id)
            self.api.consume_write_confirmation(
                self.session_id,
                confirmation_token,
                action,
            )
            status = server._submit_live_draft_pick(self.league, action)
            self.api.invalidate_draft(self.session_id)
            self._emit(
                "command_result",
                request_id=request_id,
                command="submit_pick",
                submitted=True,
                http_status=status,
                pick=preview,
            )
            return
        if name == "quit":
            self.stop_event.set()
            return
        raise ValueError(
            "command must be state, pool, preview_pick, submit_pick, or quit"
        )

    def _prepare_pick(
        self, player_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.league, draft = self.api.get_draft(
            self.session_id,
            self.args.league_id,
            self.args.year,
            True,
        )
        new_picks = self.tracker.merge_rest(draft)
        for pick in new_picks:
            self._append_ledger("SELECTED", pick)
        effective = self.tracker.effective_draft(draft)
        return server._draft_pick_action(
            self.league,
            effective,
            self.args.team_id,
            player_id,
        )

    def _publish_state(self, force: bool = False) -> None:
        state = self.tracker.snapshot()
        self._persist(state)
        compact = self._compact_state(state)
        signature = json.dumps(compact, sort_keys=True)
        if force or signature != self.last_signature:
            self.last_signature = signature
            self._emit("draft_state", state=compact)
        on_clock = compact.get("current_team_id") == self.args.team_id
        if on_clock and not self.was_on_clock:
            try:
                players = server._get_draft_pool_data(
                    self.league,
                    self.args.year,
                    self.args.on_clock_player_limit,
                    0,
                )
                self._emit("on_clock", state=compact, available_players=players)
            except Exception as error:
                self._emit("pool_error", error=self._safe_error(error))
        self.was_on_clock = on_clock

    def _compact_state(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = state or self.tracker.snapshot()
        return {
            key: value
            for key, value in state.items()
            if key != "completed_picks"
        }

    def _persist(self, state: dict[str, Any]) -> None:
        with self.persistence_lock:
            self.args.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)

    def _restore(self) -> None:
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self.tracker.restore(saved)

    def _append_ledger(self, event: str, pick: dict[str, Any] | None) -> None:
        entry = {
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": event,
            "pick": pick,
        }
        with self.persistence_lock:
            self.args.state_dir.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(entry, sort_keys=True) + "\n")

    def _stream_status(self, status: str) -> None:
        if status != self.last_stream_status:
            self.last_stream_status = status
            self._emit("stream_status", status=status)

    def _emit_ready(self) -> None:
        request = self.league.espn_request
        cookies = request.cookies or {}
        self._emit(
            "ready",
            credentials_present=bool(cookies.get("espn_s2") and cookies.get("SWID")),
            draft_writes_enabled=server._draft_write_enabled(),
            ledger_path=str(self.ledger_path),
            state_path=str(self.state_path),
            stream_enabled=not self.args.no_stream,
            team_name=self.team.team_name,
            writes_enabled=server._write_enabled(),
        )

    def _emit(self, event: str, **data: Any) -> None:
        output = {"event": event, **data}
        with self.output_lock:
            print(json.dumps(output, separators=(",", ":"), sort_keys=True), flush=True)

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, requests.RequestException):
            return f"ESPN request failed: {type(error).__name__}"
        return str(error).replace("\n", " ")[:240]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--team-id", type=int, required=True)
    parser.add_argument("--year", type=int, default=server.CURRENT_YEAR)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".draft-state")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--reconnect-interval", type=float, default=2.0)
    parser.add_argument("--on-clock-player-limit", type=int, default=20)
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    if args.league_id <= 0 or args.team_id <= 0:
        raise SystemExit("league-id and team-id must be positive")
    if args.poll_interval < 0.5:
        raise SystemExit("poll-interval must be at least 0.5 seconds")
    server._validate_draft_query(args.on_clock_player_limit, 0)
    conductor = DraftConductor(args)

    def stop(_signum: int, _frame: Any) -> None:
        conductor.stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    return conductor.run()


if __name__ == "__main__":
    raise SystemExit(main())
