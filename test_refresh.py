import copy
import unittest
from refresh import project, parse_news, validate
from uuid import uuid5, NAMESPACE_URL


def uid(label): return str(uuid5(NAMESPACE_URL, label))

class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.refs=[{'teamSeasonUid':uid('team'+str(i)), 'ownerUid':uid('owner'+str(i)), 'ownerName':'Owner', 'teamName':'Team', 'franchiseUid':uid('franchise'+str(i))} for i in (1,2)]
        self.catalog={'leagueUid':uid('league'),'season':2026,'teams':self.refs,'players':[{'playerUid':uid('player'),'name':'Example Player','position':'QB','nflTeam':'BUF'}]}
        self.config={'leagueId':'synthetic-league','leagueUid':uid('league'),'season':2026,'teams':{str(i):t['teamSeasonUid'] for i,t in enumerate(self.refs,1)},'players':{'synthetic-player':uid('player')}}
        self.league={'league_id':'synthetic-league','season':'2026','total_rosters':2,'roster_positions':['QB','BN']}
        self.state={'season':'2026','display_week':1,'season_type':'regular'}
        self.rosters=[{'roster_id':i,'settings':{'wins':0,'losses':0,'ties':0,'fpts':10,'fpts_decimal':25}} for i in (1,2)]
        self.matchups=[{'roster_id':i,'matchup_id':1,'starters':['synthetic-player'],'players':['synthetic-player'],'points':0,'starters_points':[0],'players_points':{'synthetic-player':0}} for i in (1,2)]
    def run_project(self): return project(self.config,self.catalog,self.league,self.state,self.rosters,self.matchups,[],'2026-09-13T17:00:00Z')
    def test_zero_and_manual_override(self):
        self.matchups[0]['custom_points']=8.5
        p=self.run_project();self.assertEqual(p['teams'][0]['players'][0]['points'],0);self.assertEqual(p['teams'][0]['pointsFor'],10.25)
        self.assertIn(8.5,[p['matchups'][0]['scoreA'],p['matchups'][0]['scoreB']])
        self.assertNotIn('synthetic-player',str(p));self.assertNotIn('synthetic-league',str(p))
    def test_missing_score_is_not_zero(self):
        self.matchups[0].pop('starters_points');self.matchups[0]['players_points']={}
        self.assertIsNone(self.run_project()['teams'][0]['players'][0]['points'])
    def test_unknown_player_remains_unlinked(self):
        self.config['players']={};p=self.run_project();self.assertIsNone(p['teams'][0]['players'][0]['playerUid']);self.assertEqual(p['coverage']['unresolvedPlayers'],2)
    def test_wrong_season_fails(self):
        self.state['season']='2027'
        with self.assertRaises(ValueError):self.run_project()
    def test_unknown_team_fails(self):
        self.matchups[0]['roster_id']=3
        with self.assertRaises(ValueError):self.run_project()
    def test_missing_team_fails(self):
        self.matchups.pop()
        with self.assertRaises(ValueError):self.run_project()
    def test_broken_pair_fails(self):
        self.matchups[0]['matchup_id']=2
        with self.assertRaises(ValueError):self.run_project()
    def test_starter_parity_fails(self):
        self.matchups[0]['starters_points']=[]
        with self.assertRaises(ValueError):self.run_project()
    def test_nonfinite_fails(self):
        self.matchups[0]['points']=float('nan')
        with self.assertRaises(ValueError):self.run_project()
    def test_private_field_is_rejected(self):
        p=self.run_project();p['teams'][0]['owner_id']='private'
        with self.assertRaises(ValueError):validate(p,self.catalog)
    def test_news_only_official_links(self):
        xml=b'<rss><channel><item><title>NFL update</title><link>https://www.espn.com/nfl/story/_/id/123/example</link><pubDate>Sun, 13 Sep 2026 17:00:00 GMT</pubDate></item></channel></rss>'
        self.assertEqual(parse_news(xml,'2026-09-13T17:00:00Z')['items'][0]['title'],'NFL update')
        with self.assertRaises(ValueError):parse_news(xml.replace(b'www.espn.com',b'evil.example'),'2026-09-13T17:00:00Z')

if __name__=='__main__':unittest.main()
