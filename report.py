"""Generate a static HTML report of Danish players at a Coral tournament."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page


APP_URL = "https://app.tablesoccer.org"
DBFF_ORG_ID = 853180033
STREAMING_TABLES = range(1, 9)
STREAM_URLS = {
    1: "https://www.youtube.com/watch?v=7sws9wu6ZOs",
    2: "https://www.youtube.com/watch?v=ESUcgmv6G8k",
    3: "https://www.youtube.com/watch?v=4m8s5wOrxEE",
    4: "https://www.youtube.com/watch?v=JChx-dpKezE",
    5: "https://www.youtube.com/watch?v=p9h31oWkiEM",
    6: "https://www.youtube.com/watch?v=rd-YuMnlZTE",
    7: "https://www.youtube.com/watch?v=YX-RgzwKzEY",
    8: "https://www.youtube.com/watch?v=NiuWu_Yqusk",
}
# Coral marks a match as "launched" once it is assigned to a table and started.
# We treat "launched" as live alongside the legacy "live"/"in_progress" values.
LIVE_STATUSES = ("live", "in_progress", "launched")


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


def fetch_tournament(page: Page, code: str) -> dict:
    """Navigate to a tournament and extract full state from the Vuex store."""
    page.evaluate(
        "(code) => { document.querySelector('#q-app').__vue__.$router.push('/t/' + code); }",
        code,
    )
    page.wait_for_timeout(6000)

    return page.evaluate("""() => {
        const s = document.querySelector('#q-app').__vue__.$store.state.tournament;
        const store = document.querySelector('#q-app').__vue__.$store;
        const teamPlayerMap = store.getters['tournament/teamPlayers'] || {};

        const players = {};
        for (const [pid, p] of Object.entries(s.players || {})) {
            players[pid] = {
                id: p.id,
                first_name: p.first_name,
                last_name: p.last_name,
                nationality: p.nationality,
                org_ids: (p.organizations || []).map(o => o.organization?.id),
            };
        }

        const teams = {};
        for (const [tid, t] of Object.entries(s.teams || {})) {
            teams[tid] = {
                id: t.id,
                lineup: t.lineup || [],
            };
        }

        const competitions = {};
        for (const [cid, c] of Object.entries(s.competitions || {})) {
            competitions[cid] = { id: c.id, name: c.name, status: c.status };
        }

        const phases = {};
        for (const [pid, p] of Object.entries(s.phases || {})) {
            phases[pid] = {
                id: p.id, name: p.name, status: p.status, type: p.type,
                competition_id: p.competition_id,
            };
        }

        const matches = Object.values(s.matches || {}).map(m => ({
            id: m.id, status: m.status, phase_id: m.phase_id,
            round: m.round, home: m.home, away: m.away,
            winner: m.winner, score: m.score,
            playground_id: m.playground_id,
            playground2_id: m.playground2_id,
            start_at: m.start_at, end_at: m.end_at,
        }));

        const playgrounds = {};
        for (const [pid, p] of Object.entries(s.playgrounds || {})) {
            playgrounds[pid] = { id: p.id, number: p.number, name: p.name };
        }

        const phase_teams = Object.values(s.phase_teams || {}).map(pt => ({
            phase_id: pt.phase_id, team_id: pt.team_id, rank: pt.rank,
        }));

        const competition_players = Object.values(s.competition_players || {}).map(cp => ({
            competition_id: cp.competition_id, player_id: cp.player_id,
            team_id: cp.team_id, status: cp.status,
        }));

        return {
            tournament: {
                id: s.tournament?.id, code: s.tournament?.code,
                name: s.tournament?.name, status: s.tournament?.status,
                start_at: s.tournament?.start_at, end_at: s.tournament?.end_at,
                address: s.tournament?.address,
            },
            players, teams, competitions, phases, matches, playgrounds,
            phase_teams, competition_players, teamPlayerMap,
        };
    }""")


def is_danish(player: dict) -> bool:
    if player.get("nationality") == "DK":
        return True
    if DBFF_ORG_ID in (player.get("org_ids") or []):
        return True
    return False


def team_player_ids(data: dict, team_id) -> list[int]:
    """Resolve a team ID to its player IDs using teamPlayerMap."""
    tid = str(team_id)
    if tid == "1":
        return []
    # teamPlayerMap is the authoritative source (from Vuex getter)
    tpm = data.get("teamPlayerMap") or {}
    if tid in tpm:
        return tpm[tid]
    # Fallback to teams dict
    team = data["teams"].get(tid)
    if team:
        return team.get("lineup") or []
    return []


def resolve_name(data: dict, team_id) -> str:
    tid = str(team_id)
    if tid == "1":
        return "BYE"
    pids = team_player_ids(data, team_id)
    if pids:
        names = []
        for pid in pids:
            p = data["players"].get(str(pid))
            if p:
                names.append(f"{p['first_name']} {p['last_name']}")
        if names:
            return " / ".join(names)
    p = data["players"].get(tid)
    if p:
        return f"{p['first_name']} {p['last_name']}"
    return tid


def team_has_danish(data: dict, team_id) -> bool:
    tid = str(team_id)
    if tid == "1":
        return False
    pids = team_player_ids(data, team_id)
    for pid in pids:
        p = data["players"].get(str(pid))
        if p and is_danish(p):
            return True
    return False


def table_number(match: dict, playgrounds: dict | None = None) -> int | None:
    """Return the table number a match is assigned to, via its playground_id."""
    if not playgrounds:
        return None
    pg_id = match.get("playground_id")
    if pg_id in (None, ""):
        return None
    pg = playgrounds.get(str(pg_id)) or playgrounds.get(pg_id)
    if not pg:
        return None
    number = pg.get("number")
    if isinstance(number, (int, float)):
        return int(number)
    if isinstance(number, str) and number.isdigit():
        return int(number)
    return None


def build_streaming_matches(data: dict, phase_to_comp: dict[int, int]) -> list[dict]:
    """Live Danish matches assigned to streaming tables 1–8."""
    streaming = []
    playgrounds = data.get("playgrounds") or {}
    for m in data["matches"]:
        if m.get("status") not in LIVE_STATUSES:
            continue
        table = table_number(m, playgrounds)
        if table not in STREAMING_TABLES:
            continue
        home_has_dk = team_has_danish(data, m["home"])
        away_has_dk = team_has_danish(data, m["away"])
        if not home_has_dk and not away_has_dk:
            continue
        comp_id = phase_to_comp.get(m["phase_id"])
        competition = data["competitions"].get(str(comp_id), {}).get("name", "Unknown") if comp_id else "Unknown"
        phase = data["phases"].get(str(m["phase_id"]), {}).get("name", "")
        streaming.append({
            "id": m["id"],
            "table": table,
            "competition": competition,
            "phase": phase,
            "home": resolve_name(data, m["home"]),
            "away": resolve_name(data, m["away"]),
            "home_is_dk": home_has_dk,
            "away_is_dk": away_has_dk,
            "score": fmt_score(m),
        })
    streaming.sort(key=lambda m: (m["table"], m["competition"], m["home"]))
    return streaming


def render_streaming_section(streaming_matches: list[dict] | None) -> tuple[str, str]:
    """Render the stream-table section and its JSON payload."""
    streaming_data = streaming_matches or []
    streaming_payload = json.dumps(streaming_data, ensure_ascii=False)
    stream_links = "".join(
        f'<a href="{escape(url)}" target="_blank" rel="noopener">Table {table}</a>'
        for table, url in STREAM_URLS.items()
    )
    if streaming_data:
        rows = ""
        for sm in streaming_data:
            home_cls = "stream-team dk" if sm["home_is_dk"] else "stream-team"
            away_cls = "stream-team dk" if sm["away_is_dk"] else "stream-team"
            phase = f' · {escape(sm["phase"])}' if sm.get("phase") else ""
            score = f'<span class="stream-score">{escape(sm["score"])}</span>' if sm.get("score") else ""
            table_link = STREAM_URLS.get(sm["table"])
            table = f'<a href="{escape(table_link)}" target="_blank" rel="noopener">Table {sm["table"]}</a>' if table_link else f'Table {sm["table"]}'
            match = f"""<div class="stream-comp">{escape(sm['competition'])}{phase}</div>
    <div><span class="{home_cls}">{escape(sm['home'])}</span> <span class="versus">vs</span> <span class="{away_cls}">{escape(sm['away'])}</span> {score}</div>"""
            if table_link:
                match = f'<a class="stream-match stream-match-link" href="{escape(table_link)}" target="_blank" rel="noopener">\n    {match}\n  </a>'
            else:
                match = f'<div class="stream-match">\n    {match}\n  </div>'
            rows += f"""<div class="stream-row">
  <div class="stream-table">{table}</div>
  {match}
</div>
"""
        return f"""
<section class="stream-section has-streaming" aria-live="polite">
  <div class="section-heading">
    <h2>📺 Danish players on stream tables</h2>
    <button type="button" class="notify-button" id="enable-stream-notifications">Enable notifications</button>
  </div>
  <div class="stream-links">{stream_links}</div>
  {rows}
</section>
""", streaming_payload

    return f"""
<section class="stream-section" aria-live="polite">
  <div class="section-heading">
    <h2>📺 Danish players on stream tables</h2>
    <button type="button" class="notify-button" id="enable-stream-notifications">Enable notifications</button>
  </div>
  <div class="stream-links">{stream_links}</div>
  <p class="stream-empty">No Danish players are currently live on tables 1–8.</p>
</section>
""", streaming_payload


def render_notification_script(code: str, streaming_payload: str) -> str:
    """JavaScript for opt-in notifications from the periodically updated static page."""
    return f"""<script id="streaming-matches-data" type="application/json">{streaming_payload}</script>
<script>
(function () {{
  const STORAGE_KEY = 'coral-streaming-match-ids:{code}';
  const NOTIFY_KEY = 'coral-streaming-notifications:{code}';
  const CHECK_MS = 2 * 60 * 1000;

  function currentMatches() {{
    const el = document.getElementById('streaming-matches-data');
    if (!el) return [];
    try {{ return JSON.parse(el.textContent || '[]'); }} catch (_) {{ return []; }}
  }}

  function matchId(match) {{
    return String(match.id) + ':table-' + String(match.table);
  }}

  function describe(match) {{
    return 'Table ' + match.table + ': ' + match.home + ' vs ' + match.away;
  }}

  function notificationEnabled() {{
    return localStorage.getItem(NOTIFY_KEY) === '1' &&
      'Notification' in window && Notification.permission === 'granted';
  }}

  function setButtonState() {{
    const button = document.getElementById('enable-stream-notifications');
    if (!button) return;
    if (!('Notification' in window)) {{
      button.textContent = 'Notifications unavailable';
      button.disabled = true;
    }} else if (Notification.permission === 'granted' && localStorage.getItem(NOTIFY_KEY) === '1') {{
      button.textContent = 'Notifications enabled';
      button.disabled = true;
    }} else if (Notification.permission === 'denied') {{
      button.textContent = 'Notifications blocked';
      button.disabled = true;
    }}
  }}

  function attachButton() {{
    const button = document.getElementById('enable-stream-notifications');
    if (!button || button.dataset.notificationsBound === '1') return;
    button.dataset.notificationsBound = '1';
    button.addEventListener('click', async () => {{
      if (!('Notification' in window)) return;
      const permission = await Notification.requestPermission();
      if (permission === 'granted') {{
        localStorage.setItem(NOTIFY_KEY, '1');
        remember(currentMatches());
      }}
      setButtonState();
    }});
  }}

  function remember(matches) {{
    localStorage.setItem(STORAGE_KEY, JSON.stringify(matches.map(matchId)));
  }}

  function notifyNewMatches(matches) {{
    const previous = new Set(JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'));
    const fresh = matches.filter((match) => !previous.has(matchId(match)));
    if (fresh.length && notificationEnabled()) {{
      const first = fresh[0];
      new Notification('Danish player on stream table', {{
        body: fresh.length === 1 ? describe(first) : fresh.length + ' Danish stream-table matches are live',
        tag: 'coral-streaming-{code}',
        renotify: true,
      }});
    }}
    remember(matches);
  }}

  async function checkForUpdates() {{
    try {{
      const response = await fetch(window.location.href.split('#')[0], {{ cache: 'no-store' }});
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const data = doc.getElementById('streaming-matches-data');
      if (!data) return;
      const nextSection = doc.querySelector('.stream-section');
      const currentSection = document.querySelector('.stream-section');
      if (nextSection && currentSection) {{
        currentSection.replaceWith(nextSection);
        attachButton();
        setButtonState();
      }}
      notifyNewMatches(JSON.parse(data.textContent || '[]'));
    }} catch (_) {{
      // Static hosting can be temporarily stale/unavailable; try again later.
    }}
  }}

  const initialMatches = currentMatches();
  if (!localStorage.getItem(STORAGE_KEY)) {{
    remember(initialMatches);
  }} else {{
    notifyNewMatches(initialMatches);
  }}

  attachButton();
  setButtonState();
  setInterval(checkForUpdates, CHECK_MS);
}})();
</script>"""


def _score_lists(score: dict | None) -> tuple[list, list]:
    """Normalise score to two lists (handles single-int 'first to N' format)."""
    if not score or not isinstance(score, dict):
        return [], []
    home = score.get("home")
    away = score.get("away")
    if isinstance(home, list) and isinstance(away, list):
        return home, away
    if isinstance(home, (int, float)) and isinstance(away, (int, float)):
        return [home], [away]
    return [], []


def fmt_score(match: dict, is_home: bool = True) -> str:
    home, away = _score_lists(match.get("score"))
    if not home:
        return ""
    if is_home:
        return " ".join(f"{h}-{a}" for h, a in zip(home, away))
    return " ".join(f"{a}-{h}" for h, a in zip(home, away))


def sets_won_lost(match: dict, is_home: bool) -> tuple[int, int]:
    home, away = _score_lists(match.get("score"))
    w = l = 0
    for h, a in zip(home, away):
        if is_home:
            if h > a: w += 1
            else: l += 1
        else:
            if a > h: w += 1
            else: l += 1
    return w, l


def points_won_lost(match: dict, is_home: bool) -> tuple[int, int]:
    home, away = _score_lists(match.get("score"))
    pw = sum(home if is_home else away)
    pl = sum(away if is_home else home)
    return pw, pl


def infer_competitions(data: dict) -> dict[int, int]:
    """Map phase_id -> competition_id.

    Phases sometimes lack a competition_id, and teams can appear in multiple
    competitions (e.g. a singles player's team reused in DYP).  We use:
      1. The phase's own competition_id when present.
      2. Phase-name matching against competition names.
      3. Majority-vote: for each phase, count how many of its teams belong to
         each *non-removed* competition and pick the winner.
    """
    phase_to_comp: dict[int, int] = {}

    # 1. Direct competition_id on the phase
    for pid_str, phase in data["phases"].items():
        if phase.get("competition_id"):
            phase_to_comp[phase["id"]] = phase["competition_id"]

    if len(phase_to_comp) == len(data["phases"]):
        return phase_to_comp

    # Exclude removed competitions from heuristic matching
    removed_comps = {
        int(cid) for cid, c in data["competitions"].items() if c["status"] == "removed"
    }
    active_comps = {
        int(cid): c for cid, c in data["competitions"].items()
        if c["status"] != "removed"
    }

    # Build team_id -> set of non-removed competition_ids
    team_to_comps: dict[int, Counter] = defaultdict(Counter)
    for cp in data["competition_players"]:
        cid = cp.get("competition_id")
        tid = cp.get("team_id")
        if tid and cid and cid not in removed_comps:
            team_to_comps[tid][cid] += 1

    # Build phase_id -> team_ids from phase_teams
    phase_team_ids: dict[int, set[int]] = defaultdict(set)
    for pt in data["phase_teams"]:
        phase_team_ids[pt["phase_id"]].add(pt["team_id"])

    # 2. Majority vote across teams in the phase
    for pid_str, phase in data["phases"].items():
        if phase["id"] in phase_to_comp:
            continue
        votes: Counter = Counter()
        for tid in phase_team_ids.get(phase["id"], set()):
            for cid, cnt in team_to_comps.get(tid, {}).items():
                votes[cid] += cnt
        # Fallback: also check match home/away teams
        for m in data["matches"]:
            if m["phase_id"] != phase["id"]:
                continue
            for tid in (m["home"], m["away"]):
                for cid, cnt in team_to_comps.get(tid, {}).items():
                    votes[cid] += cnt
        if votes:
            phase_to_comp[phase["id"]] = votes.most_common(1)[0][0]

    # 3. Adjacency: phases are ordered by ID in groups of
    #    (Qualifications -> Main Round -> Secondary Round) per competition.
    #    If a phase is still unmapped (e.g. "Qualifications" matching the
    #    wrong competition by name), inherit the competition from the nearest
    #    mapped phase with a higher ID (the Main Round that follows).
    sorted_phases = sorted(data["phases"].values(), key=lambda p: p["id"])
    for i, phase in enumerate(sorted_phases):
        if phase["id"] in phase_to_comp:
            continue
        # Look forward for the nearest mapped phase
        for j in range(i + 1, len(sorted_phases)):
            nxt = sorted_phases[j]
            if nxt["id"] in phase_to_comp:
                phase_to_comp[phase["id"]] = phase_to_comp[nxt["id"]]
                break

    return phase_to_comp


def get_best_rank(data: dict, team_ids: set[int], phase_ids: set[int]) -> int | None:
    """Get best (lowest) rank across phases for given team IDs."""
    best = None
    for pt in data["phase_teams"]:
        if pt["phase_id"] in phase_ids and pt["team_id"] in team_ids and pt.get("rank"):
            if best is None or pt["rank"] < best:
                best = pt["rank"]
    return best


def ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd'][n % 10] if n % 10 < 4 else 'th'}"


def round_label(n_matches: int) -> str:
    """Human-friendly knockout round name based on match count."""
    if n_matches == 1:
        return "Final"
    if n_matches == 2:
        return "Semifinal"
    if n_matches == 4:
        return "Quarterfinal"
    return f"Last {n_matches * 2}"


def round_label_relative(rnd: int, max_round: int) -> str:
    """Label a knockout round by its distance from the final."""
    diff = max_round - rnd
    if diff == 0:
        return "Final"
    if diff == 1:
        return "Semifinal"
    if diff == 2:
        return "Quarterfinal"
    return f"Round {rnd}"


def build_report(data: dict) -> str:
    """Build the full HTML report for Danish players."""
    tournament = data["tournament"]

    # If the tournament hasn't started (no matches yet), render the
    # pre-tournament registration view instead.
    if not data.get("matches"):
        return render_registrations_html(data)

    phase_to_comp = infer_competitions(data)

    # Precompute match counts per (phase_id, round) for round labelling
    round_match_counts: dict[tuple[int, int], int] = defaultdict(int)
    for m in data["matches"]:
        round_match_counts[(m["phase_id"], m.get("round", 0))] += 1

    # Precompute max positive round per phase for relative round labelling
    phase_max_round: dict[int, int] = {}
    for (pid, rnd), _ in round_match_counts.items():
        if rnd > 0:
            phase_max_round[pid] = max(phase_max_round.get(pid, 0), rnd)

    # Find all Danish player IDs (skip anonymous/removed)
    danish_pids = set()
    for pid_str, player in data["players"].items():
        if player.get("first_name") == "Anonymous":
            continue
        if is_danish(player):
            danish_pids.add(int(pid_str))

    # Find all team IDs that include a Danish player
    danish_team_ids = set()
    for tid_str, team in data["teams"].items():
        for pid in team.get("lineup") or []:
            if pid in danish_pids:
                danish_team_ids.add(int(tid_str))
    # Also add player IDs themselves (singles: team_id = player_id)
    danish_team_ids.update(danish_pids)

    # Group competitions and their phases
    comp_phases: dict[int, list[dict]] = defaultdict(list)
    for pid_str, phase in data["phases"].items():
        comp_id = phase_to_comp.get(phase["id"])
        if comp_id:
            comp_phases[comp_id].append(phase)

    # Build per-player stats across competitions
    # First, figure out which competitions each danish player/team is in
    player_comps: dict[int, set[int]] = defaultdict(set)  # player_id -> competition_ids
    for cp in data["competition_players"]:
        if cp["player_id"] in danish_pids:
            player_comps[cp["player_id"]].add(cp["competition_id"])

    # Build player sections
    player_sections = []

    for pid in sorted(danish_pids):
        player = data["players"].get(str(pid))
        if not player:
            continue
        pname = f"{player['first_name']} {player['last_name']}"

        # Find all team IDs this player belongs to (via teamPlayerMap)
        my_team_ids = set()
        tpm = data.get("teamPlayerMap") or {}
        for tid_str, player_ids in tpm.items():
            if pid in player_ids:
                my_team_ids.add(int(tid_str))
        my_team_ids.add(pid)

        # Find matches involving this player
        my_matches = []
        for m in data["matches"]:
            home_id = m["home"]
            away_id = m["away"]
            if home_id in my_team_ids or away_id in my_team_ids:
                my_matches.append(m)

        if not my_matches:
            continue

        def match_sort_key(m):
            phase = m.get("phase_id", 0)
            rnd = m.get("round", 0)
            # Winners bracket (positive) first, then losers bracket (negative)
            # Within each bracket, sort by ascending round number
            bracket = 0 if rnd >= 0 else 1
            rnd_abs = rnd if rnd >= 0 else -rnd
            return (phase, bracket, rnd_abs, m.get("start_at") or m.get("end_at") or "")

        my_matches.sort(key=match_sort_key)

        # Group by competition
        comp_matches: dict[str, list] = defaultdict(list)
        for m in my_matches:
            comp_id = phase_to_comp.get(m["phase_id"])
            comp_name = data["competitions"].get(str(comp_id), {}).get("name", "Unknown") if comp_id else "Unknown"
            phase = data["phases"].get(str(m["phase_id"]), {})
            phase_name = phase.get("name", "")
            key = f"{comp_name}"
            comp_matches[key].append(m)

        # Build competition blocks
        comp_blocks = []
        total_w = total_l = total_sw = total_sl = 0

        for comp_name, matches in comp_matches.items():
            # Determine partner (for doubles / DYP)
            sample = matches[0]
            sample_tid = sample["home"] if sample["home"] in my_team_ids else sample["away"]
            partner = None
            partner_pids = [p for p in team_player_ids(data, sample_tid) if p != pid]
            if partner_pids:
                partners = [data["players"].get(str(p)) for p in partner_pids]
                partners = [p for p in partners if p]
                if partners:
                    partner = " / ".join(f"{p['first_name']} {p['last_name']}" for p in partners)

            # Get rank for this competition's phases
            phase_ids_for_comp = set()
            comp_id = phase_to_comp.get(matches[0]["phase_id"])
            if comp_id:
                for phase in comp_phases.get(comp_id, []):
                    phase_ids_for_comp.add(phase["id"])
            rank = get_best_rank(data, my_team_ids, phase_ids_for_comp) if phase_ids_for_comp else None

            display_name = comp_name
            if partner:
                display_name += f" (with {partner})"

            match_rows = []
            cw = cl = csw = csl = 0

            # Determine which phases in this comp are qualifications
            phase_names_in_comp = set()
            for m in matches:
                p = data["phases"].get(str(m["phase_id"]), {})
                phase_names_in_comp.add(p.get("name", ""))
            has_knockout = any(
                n.lower() not in ("qualifications", "qualification", "kvalifikation")
                for n in phase_names_in_comp
            )

            prev_phase = None
            for m in matches:
                score_val = m.get("score", {})
                is_forfeit = score_val.get("forfeit") if isinstance(score_val, dict) else False
                is_home = m["home"] in my_team_ids
                winner = m.get("winner")
                finished = winner in (1, 2)
                won = finished and ((winner == 1 and is_home) or (winner == 2 and not is_home))
                opp_id = m["away"] if is_home else m["home"]
                opponent = resolve_name(data, opp_id)
                opp_is_danish = team_has_danish(data, opp_id)
                score = fmt_score(m, is_home)

                phase = data["phases"].get(str(m["phase_id"]), {})
                phase_name = phase.get("name", "")
                is_qual = phase_name.lower() in ("qualifications", "qualification", "kvalifikation")

                rnd = m.get("round", 0)
                bracket = "losers" if rnd < 0 else "winners"
                phase_key = f"{phase_name}|{bracket}"

                if phase_key != prev_phase:
                    label = phase_name
                    if phase_name == "Double elimination":
                        label = "Winners bracket" if bracket == "winners" else "Losers bracket"
                    match_rows.append(("phase_header", label, is_qual))
                    prev_phase = phase_key

                n_matches = round_match_counts.get((m["phase_id"], rnd), 0)
                # Detect group stages: all positive rounds have the same match count
                pos_counts = {round_match_counts.get((m["phase_id"], r), 0)
                              for r in range(1, 20)
                              if (m["phase_id"], r) in round_match_counts}
                is_group_stage = len(pos_counts) <= 1

                if (is_group_stage or is_qual) and rnd > 0:
                    rnd_label = f"R{rnd}"
                elif rnd > 0:
                    max_rnd = phase_max_round.get(m["phase_id"], rnd)
                    rnd_label = round_label_relative(rnd, max_rnd)
                elif rnd < 0:
                    rnd_label = round_label(n_matches)
                else:
                    rnd_label = "R0"

                if not is_forfeit and finished:
                    sw, sl = sets_won_lost(m, is_home)
                    csw += sw
                    csl += sl
                    if won:
                        cw += 1
                    else:
                        cl += 1

                if not finished:
                    status_class = "pending"
                    status_text = "…"
                elif won:
                    status_class = "win"
                    status_text = "W"
                else:
                    status_class = "loss"
                    status_text = "L"

                match_rows.append(("match", {
                    "round": rnd_label,
                    "status_class": status_class,
                    "status_text": status_text,
                    "opponent": opponent,
                    "opp_is_danish": opp_is_danish,
                    "score": score,
                    "forfeit": bool(is_forfeit),
                    "is_qual": is_qual,
                }))

            total_w += cw
            total_l += cl
            total_sw += csw
            total_sl += csl

            comp_blocks.append({
                "name": display_name,
                "rank": rank,
                "match_rows": match_rows,
                "wins": cw,
                "losses": cl,
                "sets_won": csw,
                "sets_lost": csl,
                "has_knockout": has_knockout,
            })

        player_sections.append({
            "name": pname,
            "player_id": pid,
            "comp_blocks": comp_blocks,
            "total_wins": total_w,
            "total_losses": total_l,
            "total_sets_won": total_sw,
            "total_sets_lost": total_sl,
        })

    # Sort players by total wins desc, then by name
    player_sections.sort(key=lambda p: (-p["total_wins"], p["name"]))

    # Build "still participating" summary: players with matches remaining
    # A player is still active if they have at least one competition where:
    # - the competition status is not "finished", AND
    # - they haven't been eliminated (no finished match with a loss in a knockout
    #   phase being the last match)
    # Simpler heuristic: player has pending/scheduled matches, or their competition
    # is still in progress and their last match was a win (or no matches yet).
    active_players = build_active_summary(
        data, danish_pids, phase_to_comp, round_match_counts, phase_max_round
    )

    streaming_matches = build_streaming_matches(data, phase_to_comp)

    return render_html(tournament, player_sections, active_players, streaming_matches)


def _is_qual_phase(phase_name: str) -> bool:
    return phase_name.lower() in ("qualifications", "qualification", "kvalifikation")


def _round_label_for_match(
    m: dict,
    round_match_counts: dict[tuple[int, int], int],
    phase_max_round: dict[int, int],
    is_qual: bool,
) -> str:
    """Short label for a match's round (mirrors logic in build_report)."""
    rnd = m.get("round", 0)
    phase_id = m["phase_id"]
    n_matches = round_match_counts.get((phase_id, rnd), 0)
    pos_counts = {
        round_match_counts.get((phase_id, r), 0)
        for r in range(1, 20)
        if (phase_id, r) in round_match_counts
    }
    is_group_stage = len(pos_counts) <= 1

    if (is_group_stage or is_qual) and rnd > 0:
        return f"R{rnd}"
    if rnd > 0:
        max_rnd = phase_max_round.get(phase_id, rnd)
        label = round_label_relative(rnd, max_rnd)
        return {"Final": "F", "Semifinal": "SF", "Quarterfinal": "QF"}.get(label, label)
    if rnd < 0:
        label = round_label(n_matches)
        return {"Final": "F", "Semifinal": "SF", "Quarterfinal": "QF"}.get(label, label)
    return "R0"


def build_active_summary(
    data: dict,
    danish_pids: set[int],
    phase_to_comp: dict[int, int],
    round_match_counts: dict[tuple[int, int], int],
    phase_max_round: dict[int, int],
) -> list[dict]:
    """Build a list of Danish players still actively participating."""
    tpm = data.get("teamPlayerMap") or {}

    # Map player -> team IDs
    pid_to_tids: dict[int, set[int]] = defaultdict(set)
    for tid_str, player_ids in tpm.items():
        for pid in player_ids:
            if pid in danish_pids:
                pid_to_tids[pid].add(int(tid_str))
    for pid in danish_pids:
        pid_to_tids[pid].add(pid)

    # Map competition_id -> status
    comp_status = {int(cid): c["status"] for cid, c in data["competitions"].items()}

    # Map player -> competition IDs via competition_players
    player_comp_ids: dict[int, set[int]] = defaultdict(set)
    for cp in data["competition_players"]:
        if cp["player_id"] in danish_pids:
            player_comp_ids[cp["player_id"]].add(cp["competition_id"])

    # Check which comps have pending matches for each player
    active_players = []
    for pid in sorted(danish_pids):
        player = data["players"].get(str(pid))
        if not player:
            continue
        pname = f"{player['first_name']} {player['last_name']}"
        my_tids = pid_to_tids.get(pid, {pid})

        active_comps = []
        for comp_id in sorted(player_comp_ids.get(pid, [])):
            cstatus = comp_status.get(comp_id, "")
            comp_info = data["competitions"].get(str(comp_id), {})
            comp_name = comp_info.get("name", "Unknown")

            if cstatus == "finished":
                continue

            # Only consider competitions that have actually started
            # (have at least one non-scheduled match)
            comp_has_started = any(
                m["status"] not in ("scheduled", "ready", "pending")
                for m in data["matches"]
                if phase_to_comp.get(m["phase_id"]) == comp_id
            )
            if not comp_has_started:
                continue

            # Check if player has a pending (not finished) match in this comp
            has_pending = False
            live_match = None
            next_pending = None
            last_match = None
            my_comp_matches = []
            for m in data["matches"]:
                mid_comp = phase_to_comp.get(m["phase_id"])
                if mid_comp != comp_id:
                    continue
                if m["home"] not in my_tids and m["away"] not in my_tids:
                    continue
                my_comp_matches.append(m)

                if m["status"] in LIVE_STATUSES:
                    live_match = m
                    has_pending = True
                elif m["status"] in ("scheduled", "ready", "pending"):
                    has_pending = True
                    if next_pending is None:
                        next_pending = m
                if m["status"] == "finished":
                    last_match = m

            # Determine "current" match for round/phase info
            current_match = live_match or next_pending or last_match
            round_info = None
            if current_match is not None:
                phase = data["phases"].get(str(current_match["phase_id"]), {})
                phase_name = phase.get("name", "")
                is_qual = _is_qual_phase(phase_name)
                rlabel = _round_label_for_match(
                    current_match, round_match_counts, phase_max_round, is_qual
                )
                # If we only have a finished knockout match (player won, waiting
                # for next round), advance the label one round closer to the final.
                if (
                    not live_match and not next_pending
                    and current_match is last_match
                    and not is_qual
                    and current_match.get("round", 0) > 0
                ):
                    rnd = current_match.get("round", 0)
                    phase_id = current_match["phase_id"]
                    pos_counts = {
                        round_match_counts.get((phase_id, r), 0)
                        for r in range(1, 20)
                        if (phase_id, r) in round_match_counts
                    }
                    is_group_stage = len(pos_counts) <= 1
                    if not is_group_stage:
                        max_rnd = phase_max_round.get(phase_id, rnd)
                        next_label = round_label_relative(rnd + 1, max_rnd)
                        rlabel = {"Final": "F", "Semifinal": "SF",
                                  "Quarterfinal": "QF"}.get(next_label, next_label)
                if is_qual:
                    qw = ql = 0
                    for m in my_comp_matches:
                        if m.get("status") != "finished":
                            continue
                        p = data["phases"].get(str(m["phase_id"]), {})
                        if not _is_qual_phase(p.get("name", "")):
                            continue
                        score_val = m.get("score", {})
                        if isinstance(score_val, dict) and score_val.get("forfeit"):
                            continue
                        is_home = m["home"] in my_tids
                        winner = m.get("winner")
                        if winner not in (1, 2):
                            continue
                        if (winner == 1 and is_home) or (winner == 2 and not is_home):
                            qw += 1
                        else:
                            ql += 1
                    round_info = f"Q {qw}-{ql}"
                else:
                    round_info = rlabel

            # If they have a live match, show it
            if live_match:
                is_home = live_match["home"] in my_tids
                opp_id = live_match["away"] if is_home else live_match["home"]
                opp_name = resolve_name(data, opp_id)
                score = fmt_score(live_match)
                active_comps.append({
                    "name": comp_name, "status": "live",
                    "opponent": opp_name, "score": score,
                    "round_info": round_info,
                })
            elif has_pending:
                active_comps.append({
                    "name": comp_name, "status": "playing",
                    "round_info": round_info,
                })
            elif last_match:
                # Competition still running but no pending matches — check if
                # their last match was a loss in a knockout phase (eliminated).
                # In group/qualification phases a loss doesn't eliminate you.
                is_home = last_match["home"] in my_tids
                won = (last_match.get("winner") == 1 and is_home) or (
                    last_match.get("winner") == 2 and not is_home
                )
                last_phase = data["phases"].get(str(last_match["phase_id"]), {})
                last_phase_name = (last_phase.get("name") or "").lower()
                is_qual = any(q in last_phase_name for q in ("kvalifikation", "qualification", "qualifications"))
                if won or is_qual:
                    # Won last match or still in a group/qual phase — awaiting next round
                    active_comps.append({
                        "name": comp_name, "status": "waiting",
                        "round_info": round_info,
                    })
                # else: lost in knockout — eliminated
            else:
                # No matches at all — only show if the player is drawn into
                # a phase that hasn't finished yet (i.e. still waiting for
                # their first match to be generated).
                in_active_phase = any(
                    pt["team_id"] in my_tids
                    and data["phases"].get(str(pt["phase_id"]), {}).get("status") != "finished"
                    for pt in data["phase_teams"]
                    if phase_to_comp.get(pt["phase_id"]) == comp_id
                )
                if in_active_phase:
                    active_comps.append({
                        "name": comp_name, "status": "waiting",
                        "round_info": round_info,
                    })

        if active_comps:
            active_players.append({"name": pname, "player_id": pid, "comps": active_comps})

    return active_players


def render_registrations_html(data: dict) -> str:
    """Render a pre-tournament view listing Danish registrations per competition."""
    tournament = data["tournament"]

    # Find Danish player IDs
    danish_pids = {
        int(pid) for pid, p in data["players"].items()
        if is_danish(p) and p.get("first_name") != "Anonymous"
    }

    # Group ALL registrations by (competition_id, team_id). A None team_id
    # means a solo entry (one per registration). The lineups for doubles
    # teams live only in competition_players, not in data["teams"].
    comp_team_pids: dict[int, dict] = defaultdict(lambda: {"teams": defaultdict(list), "solo": []})
    for cp in data["competition_players"]:
        if cp.get("status") == "removed":
            continue
        pid = cp.get("player_id")
        tid = cp.get("team_id")
        bucket = comp_team_pids[cp["competition_id"]]
        if tid:
            if pid not in bucket["teams"][tid]:
                bucket["teams"][tid].append(pid)
        else:
            bucket["solo"].append(pid)

    # Build per-competition entries, keeping only those with ≥1 Danish player
    comp_blocks = []
    for cid, bucket in comp_team_pids.items():
        comp = data["competitions"].get(str(cid), {})
        if comp.get("status") == "removed":
            continue
        cname = comp.get("name", "Unknown")

        entries: list[dict] = []

        # Team-based registrations
        for tid, pids in bucket["teams"].items():
            if not any(pid in danish_pids for pid in pids):
                continue
            names = []
            for pid in pids:
                p = data["players"].get(str(pid))
                if not p:
                    continue
                nm = f"{p['first_name']} {p['last_name']}"
                names.append({"name": nm, "is_dk": pid in danish_pids})
            if not names:
                continue
            dk_names = [n for n in names if n["is_dk"]]
            sort_key = min(
                (n["name"].split()[-1].lower(), n["name"].lower()) for n in dk_names
            )
            entries.append({"names": names, "sort_key": sort_key})

        # Solo registrations (singles)
        for pid in bucket["solo"]:
            if pid not in danish_pids:
                continue
            p = data["players"].get(str(pid))
            if not p:
                continue
            nm = f"{p['first_name']} {p['last_name']}"
            entries.append({
                "names": [{"name": nm, "is_dk": True}],
                "sort_key": (nm.split()[-1].lower(), nm.lower()),
            })

        if not entries:
            continue

        entries.sort(key=lambda e: e["sort_key"])
        n_danes = sum(1 for e in entries for n in e["names"] if n["is_dk"])
        comp_blocks.append({
            "name": cname,
            "entries": entries,
            "n_danes": n_danes,
            "n_entries": len(entries),
        })

    comp_blocks.sort(key=lambda c: (-c["n_danes"], c["name"]))

    total_danes = len(danish_pids)
    total_regs = sum(c["n_danes"] for c in comp_blocks)

    # Render HTML
    name = escape(tournament.get("name") or "")
    code = escape(tournament.get("code") or "")
    start = (tournament.get("start_at") or "")[:10]
    end = (tournament.get("end_at") or "")[:10]
    generated_at = datetime.now(ZoneInfo("Europe/Copenhagen")).strftime("%Y-%m-%d %H:%M %Z")
    streaming_html, streaming_payload = render_streaming_section([])
    notification_script = render_notification_script(code, streaming_payload)

    comps_html_parts = []
    for cb in comp_blocks:
        rows = ""
        for e in cb["entries"]:
            pieces = []
            for n in e["names"]:
                flag = ' <span class="dk-flag">🇩🇰</span>' if n["is_dk"] else ""
                cls = "reg-dk" if n["is_dk"] else "reg-other"
                pieces.append(f'<span class="{cls}">{escape(n["name"])}{flag}</span>')
            rows += f'<li class="reg-row">{" / ".join(pieces)}</li>\n'
        comps_html_parts.append(f"""
<div class="competition">
  <div class="comp-header">
    <h3>{escape(cb['name'])}</h3>
    <div class="comp-meta"><span class="comp-record">{cb['n_danes']} Danish · {cb['n_entries']} entries</span></div>
  </div>
  <ul class="reg-list">{rows}</ul>
</div>
""")

    comps_html = "\n".join(comps_html_parts) if comps_html_parts else \
        "<p style='text-align:center;color:var(--text-dim);padding:2rem;'>No Danish registrations found.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🇩🇰 Danish Registrations — {name}</title>
<style>
:root {{
  --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
  --text: #e4e4e7; --text-dim: #9ca3af; --accent: #3b82f6; --rank: #f59e0b;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5;
  padding: 1rem; max-width: 900px; margin: 0 auto;
}}
header {{
  text-align: center; padding: 2rem 1rem;
  border-bottom: 1px solid var(--border); margin-bottom: 1.5rem;
}}
header h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
header .meta {{ color: var(--text-dim); font-size: 0.9rem; }}
header .status {{
  display: inline-block; margin-top: 0.5rem;
  padding: 0.2rem 0.8rem; border-radius: 999px;
  font-size: 0.85rem; background: var(--card); border: 1px solid var(--border);
}}
.summary {{
  display: flex; justify-content: center; gap: 2rem;
  margin-bottom: 1.5rem; color: var(--text-dim); font-size: 0.9rem;
}}
.section-heading {{
  display: flex; align-items: center; justify-content: space-between;
  gap: 1rem; margin-bottom: 0.75rem;
}}
.stream-section {{
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 1rem; margin-bottom: 1.5rem;
}}
.stream-section.has-streaming {{
  border-color: var(--rank);
  box-shadow: 0 0 0 1px rgba(245, 158, 11, 0.25), 0 0 24px rgba(245, 158, 11, 0.08);
}}
.stream-section h2 {{ font-size: 1.1rem; }}
.stream-links {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.6rem; }}
.stream-links a, .stream-table a {{ color: var(--rank); font-weight: 700; text-decoration: none; }}
.stream-links a {{ border: 1px solid var(--border); border-radius: 999px; padding: 0.2rem 0.55rem; font-size: 0.8rem; }}
.stream-links a:hover, .stream-table a:hover {{ text-decoration: underline; }}
.stream-match-link {{ color: inherit; display: block; text-decoration: none; }}
.stream-match-link:hover .stream-team {{ text-decoration: underline; }}
.notify-button {{
  background: var(--accent); color: white; border: 0; border-radius: 999px;
  padding: 0.35rem 0.8rem; cursor: pointer; font-size: 0.8rem; white-space: nowrap;
}}
.notify-button[disabled] {{ cursor: default; opacity: 0.65; }}
.stream-row {{
  display: flex; gap: 0.75rem; padding: 0.55rem 0; border-top: 1px solid var(--border);
}}
.stream-row:first-of-type {{ border-top: none; }}
.stream-table {{ flex: 0 0 4.5rem; color: var(--rank); font-weight: 700; }}
.stream-comp {{ color: var(--text-dim); font-size: 0.8rem; }}
.stream-team.dk {{ color: var(--rank); font-weight: 700; }}
.versus, .stream-score, .stream-empty {{ color: var(--text-dim); }}
.stream-score {{ margin-left: 0.35rem; font-variant-numeric: tabular-nums; }}
.generated {{ color: var(--text-dim); font-size: 0.8rem; margin-top: 0.5rem; }}
.competition {{
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  margin-bottom: 0.75rem; padding: 0.75rem 1rem;
}}
.comp-header {{
  display: flex; justify-content: space-between; align-items: baseline;
  flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.5rem;
}}
.comp-header h3 {{ font-size: 1rem; font-weight: 600; color: var(--accent); }}
.comp-meta {{ font-size: 0.8rem; color: var(--text-dim); }}
.reg-list {{ list-style: none; padding: 0; margin: 0; }}
.reg-row {{
  padding: 0.3rem 0;
  border-top: 1px solid var(--border);
  font-size: 0.9rem;
}}
.reg-row:first-child {{ border-top: none; }}
.reg-other {{ color: var(--text-dim); }}
.dk-flag {{ font-size: 0.75rem; }}
footer {{
  text-align: center; padding: 2rem 0 1rem;
  color: var(--text-dim); font-size: 0.75rem;
}}
footer a {{ color: var(--accent); text-decoration: none; }}
@media (max-width: 600px) {{
  body {{ padding: 0.5rem; }}
  .comp-header {{ flex-direction: column; }}
  .section-heading {{ align-items: flex-start; flex-direction: column; }}
  .stream-row {{ flex-direction: column; gap: 0.25rem; }}
  .stream-table {{ flex-basis: auto; }}
}}
</style>
</head>
<body>

<header>
  <h1>🇩🇰 Danish Registrations</h1>
  <div class="meta">{name} · {start} — {end}</div>
  <div class="status">📋 Not started yet</div>
  <div class="generated">Report generated at {generated_at}</div>
</header>

<div class="summary">
  <span>{total_danes} Danish players · {total_regs} registrations</span>
  <span><a href="https://app.tablesoccer.org/p/{code}" target="_blank" style="color:var(--accent);text-decoration:none;">View on Coral ↗</a></span>
</div>

{streaming_html}

{comps_html}

<footer>
  Data from <a href="https://app.tablesoccer.org/p/{code}">app.tablesoccer.org</a>
</footer>

{notification_script}

</body>
</html>"""


def render_html(
    tournament: dict,
    player_sections: list[dict],
    active_players: list[dict] | None = None,
    streaming_matches: list[dict] | None = None,
) -> str:
    t = tournament
    name = escape(t.get("name") or "")
    code = escape(t.get("code") or "")
    status = t.get("status", "")
    start = (t.get("start_at") or "")[:10]
    end = (t.get("end_at") or "")[:10]

    status_badge = {
        "finished": "🏁 Finished",
        "in_progress": "🔴 In Progress",
        "live": "🔴 Live",
    }.get(status, status.title())

    player_html_parts = []
    for ps in player_sections:
        record = f"{ps['total_wins']}W – {ps['total_losses']}L"
        sets = f"{ps['total_sets_won']}–{ps['total_sets_lost']} sets"

        comps_html = ""
        for cb in ps["comp_blocks"]:
            rank_html = ""
            if cb["rank"]:
                rank_html = f'<span class="rank">{ordinal(cb["rank"])}</span>'

            # Split rows into qual and non-qual sections
            fold_quals = cb.get("has_knockout", False)
            qual_rows_html = ""
            main_rows_html = ""
            in_qual = False

            for row in cb["match_rows"]:
                row_type = row[0]
                if row_type == "phase_header":
                    _, label, is_q = row
                    in_qual = is_q
                    html = f'<tr class="phase-header"><td colspan="4">{escape(label)}</td></tr>\n'
                else:
                    d = row[1]
                    in_qual = d.get("is_qual", False) if not in_qual else in_qual
                    dk_badge = ' <span class="dk-flag">🇩🇰</span>' if d["opp_is_danish"] else ""
                    forfeit = ' <span class="forfeit">FF</span>' if d["forfeit"] else ""
                    html = f"""<tr>
  <td class="round">{escape(d['round'])}</td>
  <td class="result {d['status_class']}">{d['status_text']}</td>
  <td class="opponent">{escape(d['opponent'])}{dk_badge}</td>
  <td class="score">{escape(d['score'])}{forfeit}</td>
</tr>
"""
                if fold_quals and in_qual:
                    qual_rows_html += html
                else:
                    main_rows_html += html

            if fold_quals and qual_rows_html:
                qual_count = qual_rows_html.count('<td class="round">')
                rows_html = f"""<tr><td colspan="4">
<details class="qual-fold">
  <summary>Qualifications ({qual_count} matches)</summary>
  <table class="matches">{qual_rows_html}</table>
</details>
</td></tr>
{main_rows_html}"""
            else:
                rows_html = qual_rows_html + main_rows_html

            comp_record = f"{cb['wins']}W–{cb['losses']}L"
            comp_sets = f"{cb['sets_won']}–{cb['sets_lost']} sets"

            comps_html += f"""
<div class="competition">
  <div class="comp-header">
    <h3>{escape(cb['name'])}</h3>
    <div class="comp-meta">{rank_html} <span class="comp-record">{comp_record} · {comp_sets}</span></div>
  </div>
  <table class="matches">{rows_html}</table>
</div>
"""

        anchor = f"player-{ps['player_id']}"
        player_html_parts.append(f"""
<details class="player-card" id="{anchor}" open>
  <summary>
    <span class="player-name">{escape(ps['name'])}</span>
    <span class="player-record">{record} · {sets}</span>
  </summary>
  {comps_html}
</details>
""")

    players_html = "\n".join(player_html_parts)
    n_players = len(player_sections)

    # Build player jump dropdown options (sorted alphabetically by surname)
    sorted_by_surname = sorted(
        player_sections,
        key=lambda p: p["name"].split()[-1].lower() + " " + p["name"].split()[0].lower(),
    )
    player_options = "\n    ".join(
        f'<option value="#player-{ps["player_id"]}">{escape(ps["name"].split()[-1] + ", " + " ".join(ps["name"].split()[:-1]))}</option>'
        for ps in sorted_by_surname
    )

    generated_at = datetime.now(ZoneInfo("Europe/Copenhagen")).strftime("%Y-%m-%d %H:%M %Z")

    streaming_html, streaming_payload = render_streaming_section(streaming_matches)
    notification_script = render_notification_script(code, streaming_payload)

    # Build active players summary
    active_html = ""
    if active_players:
        rows = ""
        for ap in active_players:
            badges = []
            for c in ap["comps"]:
                rinfo = c.get("round_info")
                rprefix = f'<span class="round-tag">{escape(rinfo)}</span>' if rinfo else ""
                if c["status"] == "live":
                    label = f'{c["name"]} vs {c["opponent"]}'
                    if c.get("score"):
                        label += f' ({c["score"]})'
                    inner = f'<span class="active-comp live">🔴 {escape(label)}</span>'
                elif c["status"] == "playing":
                    inner = f'<span class="active-comp playing">⚡ {escape(c["name"])}</span>'
                else:
                    inner = f'<span class="active-comp waiting">⏳ {escape(c["name"])}</span>'
                badges.append(f'<span class="active-pair">{rprefix}{inner}</span>')
            comp_badges = " ".join(badges)
            anchor = f"player-{ap['player_id']}"
            rows += f'<div class="active-row"><a class="active-name" href="#{anchor}">{escape(ap["name"])}</a>{comp_badges}</div>\n'

        active_html = f"""
<div class="active-section">
  <h2>🟢 Still Participating ({len(active_players)})</h2>
  {rows}
</div>
"""
    elif status in ("in_progress", "live"):
        active_html = """
<div class="active-section">
  <h2>🏁 No Danish players remaining</h2>
</div>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🇩🇰 Danish Players — {name}</title>
<style>
:root {{
  --bg: #0f1117;
  --card: #1a1d27;
  --border: #2a2d3a;
  --text: #e4e4e7;
  --text-dim: #9ca3af;
  --win: #22c55e;
  --loss: #ef4444;
  --accent: #3b82f6;
  --rank: #f59e0b;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  padding: 1rem;
  max-width: 900px;
  margin: 0 auto;
}}
header {{
  text-align: center;
  padding: 2rem 1rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 1.5rem;
}}
header h1 {{
  font-size: 1.6rem;
  margin-bottom: 0.25rem;
}}
header .meta {{
  color: var(--text-dim);
  font-size: 0.9rem;
}}
header .status {{
  display: inline-block;
  margin-top: 0.5rem;
  padding: 0.2rem 0.8rem;
  border-radius: 999px;
  font-size: 0.85rem;
  background: var(--card);
  border: 1px solid var(--border);
}}
.summary {{
  display: flex;
  justify-content: center;
  gap: 2rem;
  margin-bottom: 1.5rem;
  color: var(--text-dim);
  font-size: 0.9rem;
}}
.section-heading {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.75rem;
}}
.stream-section {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1.5rem;
}}
.stream-section.has-streaming {{
  border-color: var(--rank);
  box-shadow: 0 0 0 1px rgba(245, 158, 11, 0.25), 0 0 24px rgba(245, 158, 11, 0.08);
}}
.stream-section h2 {{
  font-size: 1.1rem;
}}
.stream-links {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.6rem;
}}
.stream-links a, .stream-table a {{
  color: var(--rank);
  font-weight: 700;
  text-decoration: none;
}}
.stream-links a {{
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.2rem 0.55rem;
  font-size: 0.8rem;
}}
.stream-links a:hover, .stream-table a:hover {{
  text-decoration: underline;
}}
.stream-match-link {{
  color: inherit;
  display: block;
  text-decoration: none;
}}
.stream-match-link:hover .stream-team {{
  text-decoration: underline;
}}
.notify-button {{
  background: var(--accent);
  color: white;
  border: 0;
  border-radius: 999px;
  padding: 0.35rem 0.8rem;
  cursor: pointer;
  font-size: 0.8rem;
  white-space: nowrap;
}}
.notify-button[disabled] {{
  cursor: default;
  opacity: 0.65;
}}
.stream-row {{
  display: flex;
  gap: 0.75rem;
  padding: 0.55rem 0;
  border-top: 1px solid var(--border);
}}
.stream-row:first-of-type {{ border-top: none; }}
.stream-table {{
  flex: 0 0 4.5rem;
  color: var(--rank);
  font-weight: 700;
}}
.stream-comp {{
  color: var(--text-dim);
  font-size: 0.8rem;
}}
.stream-team.dk {{
  color: var(--rank);
  font-weight: 700;
}}
.versus, .stream-score, .stream-empty {{
  color: var(--text-dim);
}}
.stream-score {{
  margin-left: 0.35rem;
  font-variant-numeric: tabular-nums;
}}
.player-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 0.75rem;
  overflow: hidden;
}}
.player-card > summary {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.75rem 1rem;
  cursor: pointer;
  user-select: none;
  list-style: none;
}}
.player-card > summary::-webkit-details-marker {{ display: none; }}
.player-card > summary::before {{
  content: '▶';
  margin-right: 0.5rem;
  font-size: 0.7rem;
  color: var(--text-dim);
  transition: transform 0.2s;
}}
.player-card[open] > summary::before {{ transform: rotate(90deg); }}
.player-name {{ font-weight: 600; font-size: 1.05rem; }}
.player-record {{ color: var(--text-dim); font-size: 0.85rem; white-space: nowrap; }}
.competition {{
  padding: 0.5rem 1rem 0.75rem;
  border-top: 1px solid var(--border);
}}
.comp-header {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}}
.comp-header h3 {{ font-size: 0.95rem; font-weight: 500; color: var(--accent); }}
.comp-meta {{ font-size: 0.8rem; color: var(--text-dim); display: flex; gap: 0.5rem; align-items: baseline; }}
.rank {{
  background: var(--rank);
  color: #000;
  font-weight: 700;
  padding: 0.1rem 0.5rem;
  border-radius: 4px;
  font-size: 0.8rem;
}}
.comp-record {{ white-space: nowrap; }}
table.matches {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}}
table.matches td {{ padding: 0.25rem 0.4rem; }}
.phase-header td {{
  font-weight: 600;
  font-size: 0.8rem;
  color: var(--text-dim);
  padding-top: 0.5rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}}
.round {{ color: var(--text-dim); width: 5rem; white-space: nowrap; }}
.result {{ width: 2rem; text-align: center; font-weight: 700; border-radius: 3px; }}
.result.win {{ color: var(--win); }}
.result.loss {{ color: var(--loss); }}
.result.pending {{ color: var(--text-dim); }}
.opponent {{ }}
.score {{ text-align: right; font-variant-numeric: tabular-nums; color: var(--text-dim); white-space: nowrap; }}
.dk-flag {{ font-size: 0.75rem; }}
.forfeit {{ font-size: 0.7rem; color: var(--text-dim); font-style: italic; }}
.qual-fold {{
  margin: 0.25rem 0;
}}
.qual-fold > summary {{
  cursor: pointer;
  color: var(--text-dim);
  font-size: 0.8rem;
  padding: 0.25rem 0;
  user-select: none;
}}
.qual-fold > summary:hover {{
  color: var(--text);
}}
.qual-fold table.matches {{
  margin-top: 0.25rem;
}}
.active-section {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1.5rem;
}}
.active-section h2 {{
  font-size: 1.1rem;
  margin-bottom: 0.75rem;
}}
.active-row {{
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem;
  padding: 0.3rem 0;
}}
a.active-name {{
  font-weight: 600;
  min-width: 10rem;
  margin-right: 0.5rem;
  color: var(--text);
  text-decoration: none;
}}
a.active-name:hover {{
  color: var(--accent);
}}
.active-pair {{
  display: inline-flex;
  align-items: stretch;
  white-space: nowrap;
}}
.round-tag {{
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.45rem;
  border-radius: 999px 0 0 999px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--text-dim);
  border: 1px solid var(--border);
  border-right: 0;
  letter-spacing: 0.02em;
  display: inline-flex;
  align-items: center;
}}
.active-comp {{
  font-size: 0.8rem;
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  white-space: nowrap;
}}
.active-pair .active-comp {{
  border-radius: 0 999px 999px 0;
}}
.active-comp.playing {{
  background: rgba(34, 197, 94, 0.15);
  border: 1px solid var(--win);
  color: var(--win);
}}
.active-comp.live {{
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid var(--loss);
  color: var(--loss);
  font-weight: 600;
}}
.active-comp.waiting {{
  background: rgba(245, 158, 11, 0.15);
  border: 1px solid var(--rank);
  color: var(--rank);
}}
footer {{
  text-align: center;
  padding: 2rem 0 1rem;
  color: var(--text-dim);
  font-size: 0.75rem;
}}
footer a {{ color: var(--accent); text-decoration: none; }}
.player-jump {{
  text-align: center;
  margin-bottom: 1.5rem;
}}
.player-jump select {{
  background: var(--card);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.5rem 1rem;
  font-size: 0.9rem;
  cursor: pointer;
  width: 100%;
  max-width: 400px;
}}
.player-jump select:focus {{
  outline: none;
  border-color: var(--accent);
}}
.generated {{
  color: var(--text-dim);
  font-size: 0.8rem;
  margin-top: 0.5rem;
}}
@media (max-width: 600px) {{
  body {{ padding: 0.5rem; }}
  .comp-header {{ flex-direction: column; }}
  .section-heading {{ align-items: flex-start; flex-direction: column; }}
  .stream-row {{ flex-direction: column; gap: 0.25rem; }}
  .stream-table {{ flex-basis: auto; }}
}}
</style>
</head>
<body>

<header>
  <h1>🇩🇰 Danish Players</h1>
  <div class="meta">{name} · {start} — {end}</div>
  <div class="status">{status_badge}</div>
  <div class="generated">Report generated at {generated_at}</div>
</header>

<div class="summary">
  <span>{n_players} Danish players tracked</span>
  <span><a href="https://app.tablesoccer.org/p/{code}" target="_blank">View on Coral ↗</a></span>
</div>

<div class="player-jump">
  <select onchange="if(this.value)location.hash=this.value;this.selectedIndex=0;">
    <option value="">Jump to player…</option>
    {player_options}
  </select>
</div>

{streaming_html}

{active_html}

{players_html}

<footer>
  Data from <a href="https://app.tablesoccer.org/p/{code}">app.tablesoccer.org</a>
</footer>

{notification_script}

</body>
</html>"""


def main() -> None:
    load_dotenv()
    username = os.environ["CORAL_USERNAME"]
    password = os.environ["CORAL_PASSWORD"]

    if len(sys.argv) < 2:
        print("Usage: report.py <TOURNAMENT_CODE> [output.html]")
        sys.exit(1)

    code = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else "docs/index.html"

    print(f"Logging in...")
    with sync_playwright() as pw:
        browser, page = login(pw, username, password)
        try:
            print(f"Fetching tournament {code}...")
            data = fetch_tournament(page, code)
        finally:
            browser.close()

    print(f"Building report...")
    html = build_report(data)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {output} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
