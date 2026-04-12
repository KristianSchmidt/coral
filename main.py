from __future__ import annotations

import json
import os
import sys
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page

APP_URL = "https://app.tablesoccer.org"


def login(pw, username: str, password: str) -> tuple:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()

    page.goto(f"{APP_URL}/login")
    page.wait_for_load_state("networkidle")

    page.fill('input[type="text"]', username)
    page.click("button:has-text('Next')")
    page.locator('input[type="password"]:visible').wait_for(state="visible")
    page.locator('input[type="password"]:visible').fill(password)
    page.click("button:has-text('Sign in')")
    page.wait_for_url("**/profile**", timeout=15000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    return browser, page


def get_profile(page: Page) -> dict:
    return page.evaluate("""() => {
        const store = document.querySelector('#q-app').__vue__.$store;
        return store.state.player.profile;
    }""")


def get_tournament(page: Page, code: str) -> dict:
    """Navigate to a tournament and return its full state."""
    page.evaluate(
        "(code) => { document.querySelector('#q-app').__vue__.$router.push('/t/' + code); }",
        code,
    )
    page.wait_for_timeout(6000)

    return page.evaluate("""() => {
        const s = document.querySelector('#q-app').__vue__.$store.state.tournament;
        const players = s.players || {};
        const teams = s.teams || {};
        const competitions = s.competitions || {};
        const phases = s.phases || {};
        const matches = s.matches || {};

        // Use the app's teamPlayers getter to map team IDs -> player IDs
        const store = document.querySelector('#q-app').__vue__.$store;
        const teamPlayerMap = store.getters['tournament/teamPlayers'] || {};

        const teamLookup = {};
        for (const [tid, playerIds] of Object.entries(teamPlayerMap)) {
            const names = (playerIds || [])
                .map(pid => players[pid])
                .filter(Boolean)
                .map(p => p.first_name + ' ' + p.last_name);
            teamLookup[tid] = names.length ? names.join(' / ') : tid;
        }

        // Also add individual players (for singles where team_id = player_id)
        for (const [pid, p] of Object.entries(players)) {
            if (!teamLookup[pid]) {
                teamLookup[pid] = p.first_name + ' ' + p.last_name;
            }
        }

        return {
            tournament: {
                id: s.tournament?.id,
                code: s.tournament?.code,
                name: s.tournament?.name,
                status: s.tournament?.status,
                start_at: s.tournament?.start_at,
                end_at: s.tournament?.end_at,
            },
            competitions: Object.values(competitions).map(c => ({
                id: c.id, name: c.name, status: c.status,
            })),
            phases: Object.values(phases).map(p => ({
                id: p.id, name: p.name, status: p.status, type: p.type,
                competition_id: p.competition_id,
            })),
            teamLookup,
            playerCount: Object.keys(players).length,
            matches: Object.values(matches).map(m => ({
                id: m.id,
                status: m.status,
                phase_id: m.phase_id,
                round: m.round,
                home: m.home,
                away: m.away,
                winner: m.winner,
                score: m.score,
                start_at: m.start_at,
                end_at: m.end_at,
            })),
        };
    }""")


def resolve_name(data: dict, team_or_player_id) -> str:
    if team_or_player_id in (1, "1"):
        return "BYE"
    return data["teamLookup"].get(str(team_or_player_id), str(team_or_player_id))


def format_score(match: dict) -> str:
    score = match.get("score")
    if not score:
        return ""
    home_sets = score.get("home") or []
    away_sets = score.get("away") or []
    if not home_sets:
        return ""
    return "  ".join(f"{h}-{a}" for h, a in zip(home_sets, away_sets))


def print_profile(profile: dict) -> None:
    print(f"{'Name:':<20} {profile['first_name']} {profile['last_name']}")
    print(f"{'Code:':<20} {profile['code']}")
    print(f"{'Nationality:':<20} {profile['nationality']}")
    print(f"{'Status:':<20} {profile['status']}")
    for org in profile.get("organizations") or []:
        o = org.get("organization", {})
        print(f"{'Organization:':<20} {o.get('name')} ({o.get('short_name')})")


def print_tournament(data: dict) -> None:
    t = data["tournament"]
    print(f"{'Name:':<20} {t['name']}")
    print(f"{'Code:':<20} {t['code']}")
    print(f"{'Status:':<20} {t['status']}")
    print(f"{'Start:':<20} {t['start_at']}")
    print(f"{'End:':<20} {t['end_at']}")
    print(f"{'Players:':<20} {data['playerCount']}")
    print(f"{'Competitions:':<20} {len(data['competitions'])}")
    print(f"{'Matches:':<20} {len(data['matches'])}")
    print()

    for comp in data["competitions"]:
        print(f"  [{comp['status']:<10}] {comp['name']}")

    finished = [m for m in data["matches"] if m["status"] == "finished"]
    in_progress = [m for m in data["matches"] if m["status"] in ("live", "in_progress")]

    if in_progress:
        print(f"\n  🔴 {len(in_progress)} match(es) in progress:")
        for m in in_progress:
            home = resolve_name(data, m["home"])
            away = resolve_name(data, m["away"])
            score = format_score(m)
            print(f"    {home} vs {away}  {score}")

    if finished:
        n = min(10, len(finished))
        print(f"\n  Last {n} finished matches:")
        for m in finished[-n:]:
            home = resolve_name(data, m["home"])
            away = resolve_name(data, m["away"])
            score = format_score(m)
            w = "◀" if m.get("winner") == 1 else "▶" if m.get("winner") == 2 else " "
            print(f"    {home:<40} {score:<20} {w} {away}")


def watch_tournament(page: Page, code: str, interval: int = 30) -> None:
    """Continuously poll tournament state."""
    print(f"Watching tournament {code} (polling every {interval}s, Ctrl+C to stop)\n")
    prev_finished_ids: set[int] = set()

    while True:
        data = get_tournament(page, code)
        finished = [m for m in data["matches"] if m["status"] == "finished"]
        in_progress = [m for m in data["matches"] if m["status"] in ("live", "in_progress")]

        finished_ids = {m["id"] for m in finished}
        new_ids = finished_ids - prev_finished_ids

        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {data['tournament']['name']} — "
              f"{len(finished)} finished, {len(in_progress)} live, "
              f"{len(data['matches'])} total")

        if new_ids:
            new_matches = [m for m in finished if m["id"] in new_ids]
            for m in new_matches:
                home = resolve_name(data, m["home"])
                away = resolve_name(data, m["away"])
                score = format_score(m)
                w = "◀" if m.get("winner") == 1 else "▶" if m.get("winner") == 2 else " "
                print(f"  NEW: {home:<35} {score:<20} {w} {away}")

        if in_progress:
            for m in in_progress:
                home = resolve_name(data, m["home"])
                away = resolve_name(data, m["away"])
                score = format_score(m)
                print(f"  LIVE: {home} vs {away}  {score}")

        prev_finished_ids = finished_ids
        time.sleep(interval)


def main() -> None:
    load_dotenv()
    username = os.environ["CORAL_USERNAME"]
    password = os.environ["CORAL_PASSWORD"]

    with sync_playwright() as pw:
        browser, page = login(pw, username, password)
        try:
            if len(sys.argv) < 2 or sys.argv[1] == "profile":
                print("=== Profile ===")
                print_profile(get_profile(page))
            elif sys.argv[1] == "tournament" and len(sys.argv) > 2:
                code = sys.argv[2]
                data = get_tournament(page, code)
                print(f"=== Tournament {code} ===")
                print_tournament(data)
            elif sys.argv[1] == "watch" and len(sys.argv) > 2:
                code = sys.argv[2]
                interval = int(sys.argv[3]) if len(sys.argv) > 3 else 30
                watch_tournament(page, code, interval)
            elif sys.argv[1] == "dump" and len(sys.argv) > 2:
                code = sys.argv[2]
                data = get_tournament(page, code)
                print(json.dumps(data, indent=2, default=str))
            else:
                print("Usage:")
                print("  main.py                         Show profile")
                print("  main.py profile                 Show profile")
                print("  main.py tournament <CODE>       Show tournament summary")
                print("  main.py watch <CODE> [SEC]      Watch live tournament")
                print("  main.py dump <CODE>             Dump full tournament JSON")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
