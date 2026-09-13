"""Bounded server-only fetch; only closed canonical projections leave this process.

No raw response, credentials, provider identifiers, or exception bodies are logged,
written, cached, committed, or uploaded. The private map is a repository secret.
"""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import UUID, uuid5
import xml.etree.ElementTree as ET
import zlib

NAMESPACE = UUID('03f007fb-1dc9-562f-85ae-6f82257f50eb')
API = 'https://api.sleeper.app/v1/'
NEWS = 'https://www.espn.com/espn/rss/nfl/news'
MAX_BYTES = 2_000_000


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def fetch(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; TatnallLegacy/1.0)', 'Accept': 'application/rss+xml, application/xml, text/xml' if url == NEWS else 'application/json'})
    with urlopen(request, timeout=20) as response:
        body = response.read(MAX_BYTES + 1)
        if url == NEWS:
            print(f'News response: HTTP {response.status}; bytes {len(body)}')
        if len(body) > MAX_BYTES:
            raise ValueError('Response limit exceeded')
        return body


def number(value, nullable=True):
    if value is None and nullable:
        return None
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError('Invalid score')
    return value


def integer(value, low=0, high=1_000_000):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Invalid count')
    return value


def canonical(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Invalid canonical identity')
    return value


def safe_text(value, limit=300):
    if not isinstance(value, str) or len(value) > limit or re.search(r'[\x00-\x1f]|\d{16,}|<[^>]*>', value):
        raise ValueError('Invalid public text')
    return value


def iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def parse_news(body, captured):
    # Titles and original ESPN links only; no article bodies, images, or identity joins.
    root = ET.fromstring(body)
    rows = []
    for item in root.findall('./channel/item')[:20]:
        title = safe_text(item.findtext('title') or '')
        url = item.findtext('link') or ''
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('www.espn.com', 'espn.com') or parsed.username or parsed.password:
            raise ValueError('Unapproved news URL')
        published = iso(parsedate_to_datetime(item.findtext('pubDate') or ''))
        rows.append({'title': title, 'url': url, 'publishedAt': published})
    if not rows:
        raise ValueError('Empty news feed')
    return {'source': 'ESPN', 'status': 'available', 'capturedAt': captured, 'items': rows}


def project(config, catalog, league, state, rosters, matchups, transactions, captured):
    season = integer(config['season'], 2026, 2026)
    if str(league.get('league_id')) != config['leagueId'] or str(league.get('season')) != str(season) or str(state.get('season')) != str(season):
        raise ValueError('Source scope mismatch')
    week = integer(state.get('display_week') or state.get('week'), 1, 18)
    if state.get('season_type') not in ('regular', 'post', 'off'):
        raise ValueError('Unaccepted season type')
    if catalog['season'] != season or config['leagueUid'] != catalog['leagueUid']:
        raise ValueError('Catalog scope mismatch')
    refs = {canonical(t['teamSeasonUid']): t for t in catalog['teams']}
    players = {canonical(p['playerUid']): p for p in catalog['players']}
    if set(config['teams'].values()) != set(refs) or len(refs) != league.get('total_rosters'):
        raise ValueError('Incomplete team map')
    if len(set(config['players'].values())) != len(config['players']) or not set(config['players'].values()) <= set(players):
        raise ValueError('Ambiguous player map')
    unknown = 0
    def team(source):
        uid = config['teams'].get(str(source))
        if uid not in refs:
            raise ValueError('Unmapped team')
        return uid
    def player(source):
        nonlocal unknown
        uid = config['players'].get(str(source))
        if uid is None:
            unknown += str(source) not in ('0', '')
            return {'playerUid': None, 'name': 'Player identity unavailable', 'position': None, 'nflTeam': None}
        p = players[uid]
        return {k: p[k] for k in ('playerUid', 'name', 'position', 'nflTeam')}
    raw_by_team = {team(m['roster_id']): m for m in matchups}
    if len(raw_by_team) != len(matchups) or set(raw_by_team) != set(refs):
        raise ValueError('Incomplete matchup response')
    if len(rosters) != len(refs) or {team(r['roster_id']) for r in rosters} != set(refs):
        raise ValueError('Incomplete roster response')
    slots = [s for s in league['roster_positions'] if s not in ('BN', 'IR', 'TAXI')]
    teams = []
    for raw in rosters:
        uid = team(raw['roster_id']); matchup = raw_by_team[uid]; settings = raw['settings']
        starts = matchup.get('starters') or []
        if len(starts) != len(slots) or len(set(p for p in starts if str(p) != '0')) != len([p for p in starts if str(p) != '0']):
            raise ValueError('Invalid starter slots')
        totals = matchup.get('players_points') or {}
        starter_points = matchup.get('starters_points')
        if starter_points is not None and len(starter_points) != len(starts):
            raise ValueError('Starter score parity failed')
        entries = []
        for i, source in enumerate(starts):
            score = starter_points[i] if starter_points is not None else totals.get(str(source))
            entries.append({**player(source), 'started': True, 'slot': safe_text(slots[i], 15), 'points': number(score)})
        for source in matchup.get('players') or []:
            if source not in starts:
                entries.append({**player(source), 'started': False, 'slot': 'BN', 'points': number(totals.get(str(source)))})
        if len(entries) > 60:
            raise ValueError('Roster limit exceeded')
        teams.append({'teamSeasonUid': uid, 'lineupUid': str(uuid5(NAMESPACE, f"lineup:sleeper:{config['leagueId']}:{season}:{week}:{raw['roster_id']}")), 'wins': integer(settings['wins']), 'losses': integer(settings['losses']), 'ties': integer(settings['ties']),
                      'pointsFor': round(number(settings.get('fpts', 0), False) + number(settings.get('fpts_decimal', 0), False) / 100, 2),
                      'players': entries})
    groups = {}
    for raw in matchups:
        if raw.get('matchup_id') is not None:
            groups.setdefault(str(raw['matchup_id']), []).append(raw)
    games = []
    for pair in groups.values():
        if len(pair) != 2:
            raise ValueError('Matchup pair parity failed')
        pair.sort(key=lambda row: team(row['roster_id']))
        a, b = pair; au, bu = team(a['roster_id']), team(b['roster_id'])
        key = f"matchup:sleeper:{config['leagueId']}:{season}:{week}:{au}:{bu}"
        def points(row):
            return number(row['custom_points'] if row.get('custom_points') is not None else row.get('points'))
        games.append({'matchupUid': str(uuid5(NAMESPACE, key)), 'teamAUid': au, 'teamBUid': bu, 'scoreA': points(a), 'scoreB': points(b)})
    moves = []
    for raw in transactions:
        if raw.get('type') not in ('trade', 'waiver', 'free_agent') or raw.get('status') != 'complete':
            continue
        key = f"transaction:sleeper:{config['leagueId']}:{season}:{raw['transaction_id']}"
        assets = []
        for field in ('adds', 'drops'):
            for source, roster in (raw.get(field) or {}).items():
                p = player(source)
                assets.append({'type': 'player', 'playerUid': p['playerUid'], 'name': p['name'], 'position': p['position'], 'amount': None,
                               'from': refs[team(roster)] if field == 'drops' else None, 'to': refs[team(roster)] if field == 'adds' else None})
        moves.append({'transactionUid': str(uuid5(NAMESPACE, key)), 'week': week, 'type': raw['type'], 'status': 'complete',
                      'createdAt': iso(datetime.fromtimestamp(number(raw['created'], False) / 1000, timezone.utc)),
                      'waiverBid': number((raw.get('settings') or {}).get('waiver_bid')), 'teams': [refs[team(t)] for t in (raw.get('roster_ids') or [])], 'assets': assets})
    result = {'schemaVersion': 1, 'source': 'Sleeper', 'capturedAt': captured, 'season': season, 'week': week, 'leagueUid': catalog['leagueUid'],
              'teams': teams, 'matchups': games, 'transactions': sorted(moves, key=lambda r: r['createdAt'], reverse=True),
              'coverage': {'unresolvedPlayers': unknown, 'transactionAssets': 'Player additions and drops; draft-pick and budget transfers are not included.'},
              'provenance': {'sourceSha256': digest([league, state, rosters, matchups, transactions]), 'identityMapSha256': digest(config)}}
    validate(result, catalog)
    return result


def validate(payload, catalog):
    # Serialization is a second boundary, independent of provider response shape.
    encoded = encode(payload)
    if len(encoded) > 200_000:
        raise ValueError('Public output limit exceeded')
    allowed = {'schemaVersion','source','capturedAt','season','week','leagueUid','teams','matchups','transactions','coverage','provenance','news'}
    if set(payload) - allowed:
        raise ValueError('Unexpected top-level key')
    team_ids = {t['teamSeasonUid'] for t in catalog['teams']}; player_ids = {p['playerUid'] for p in catalog['players']}
    forbidden = re.compile(r'(token|cookie|password|secret|provider.?id|account|owner_id|roster_id|league_id|player_id)', re.I)
    def walk(value, key=''):
        if isinstance(value, dict):
            for k, v in value.items():
                if forbidden.search(k): raise ValueError('Private field')
                walk(v, k)
        elif isinstance(value, list):
            for v in value: walk(v, key)
        elif isinstance(value, str):
            if key.endswith('Sha256'):
                if not re.fullmatch('[a-f0-9]{64}', value): raise ValueError('Invalid hash')
            elif key.endswith('Uid') and key != 'leagueUid':
                canonical(value)
                if key in ('teamSeasonUid','teamAUid','teamBUid') and value not in team_ids: raise ValueError('Unknown team')
                if key == 'playerUid' and value not in player_ids: raise ValueError('Unknown player')
            else: safe_text(value, 500)
        elif isinstance(value, (float,int)) and not isinstance(value,bool): number(value, False)
    walk(payload)


def main():
    config = json.loads(zlib.decompress(base64.b64decode(os.environ['SOURCE_MAP'])))
    catalog = json.loads(Path('catalog.json').read_text())
    def get(tail): return json.loads(fetch(API + tail))
    league_path = 'league/' + config['leagueId']
    with ThreadPoolExecutor(max_workers=3) as pool:
        league, state, rosters = list(pool.map(get, [league_path, 'state/nfl', league_path + '/rosters']))
    week = integer(state.get('display_week') or state.get('week'), 1, 18)
    with ThreadPoolExecutor(max_workers=2) as pool:
        matchups, transactions = list(pool.map(get, [league_path + '/matchups/' + str(week), league_path + '/transactions/' + str(week)]))
    captured = iso(datetime.now(timezone.utc))
    result = project(config, catalog, league, state, rosters, matchups, transactions, captured)
    try:
        result['news'] = parse_news(fetch(NEWS), captured)
    except Exception as error:
        print('News unavailable: ' + type(error).__name__ + (' code ' + str(error.code) if hasattr(error, 'code') else ''))
        result['news'] = {'source': 'ESPN', 'status': 'unavailable', 'capturedAt': None, 'items': []}
    validate(result, catalog)
    output = Path('site'); output.mkdir(exist_ok=True)
    (output / 'snapshot.json').write_bytes(encode(result) + b'\n')
    (output / '.nojekyll').touch()
    print(f"Validated {len(result['teams'])} teams, {len(result['matchups'])} matchups; unresolved player references: {result['coverage']['unresolvedPlayers']}; news: {result['news']['status']}.")
    print('Public SHA-256: ' + hashlib.sha256((output / 'snapshot.json').read_bytes()).hexdigest())


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never allow urllib/provider exceptions or secret-derived assertions into public logs.
        print('Refresh validation failed. Existing published snapshot remains available.', file=sys.stderr)
        sys.exit(1)
