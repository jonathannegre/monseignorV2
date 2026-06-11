import unittest

from scripts.broker_stop_auditor import audit_protective_stops, build_stop_payload
from scripts.position_rotation import plan_rotation, score_position
from scripts.performance_attribution import attribute_closed_trades
from scripts.sec_filings_feed import classify_filing, normalize_filing
from scripts.catalyst_agent import CatalystAgent, NewsItem
from scripts.portfolio_constructor import construct_portfolio
import datetime as dt
import json
import pathlib


class MonseignorV2ControlTests(unittest.TestCase):
    def test_stop_audit_detects_missing_and_builds_repair_payload(self):
        positions = [{'symbol': 'XLF', 'qty': '12.5', 'avg_entry_price': '50', 'current_price': '53', 'market_value': '662.5'}]
        result = audit_protective_stops(positions, [], {'position_manager': {'default_stop_pct': 3.0, 'breakeven_after_r': 1.0}})
        self.assertTrue(result['critical_incident'])
        self.assertEqual(result['missing_stop_count'], 1)
        row = result['rows'][0]
        self.assertEqual(row['repair_payload']['time_in_force'], 'day')
        self.assertEqual(row['repair_payload']['type'], 'stop')
        self.assertGreaterEqual(row['desired_stop_price'], 50.0)

    def test_stop_audit_accepts_visible_protective_stop(self):
        positions = [{'symbol': 'XLF', 'qty': '10', 'avg_entry_price': '50', 'current_price': '49'}]
        orders = [{'symbol': 'XLF', 'side': 'sell', 'type': 'stop', 'stop_price': '48.50'}]
        result = audit_protective_stops(positions, orders, {'position_manager': {'default_stop_pct': 3.0}})
        self.assertTrue(result['all_positions_protected'])
        self.assertEqual(result['rows'][0]['status'], 'protected')
        self.assertEqual(build_stop_payload('XLF', 10, 48.5)['time_in_force'], 'gtc')

    def test_position_rotation_recommends_exit_replace_and_blocks_micro_buys(self):
        positions = [{'symbol': 'VZ', 'qty': '20', 'avg_entry_price': '40', 'current_price': '39', 'market_value': '780'}]
        candidates = [{'symbol': 'NVDA', 'confidence': 9, 'risk_reward': 2.5, 'catalyst_score': 8.5}]
        catalyst = {'VZ': {'score': 2, 'trade_allowed': False, 'catalyst_status': 'negative_news_veto'}}
        result = plan_rotation(positions, candidates, {'cash': 5, 'portfolio_value': 1000}, {'cash_control': {'min_new_buy_cash_usd': 50}, 'position_rotation': {'replace_score_margin': 1.0}}, catalyst)
        self.assertEqual(result['mode'], 'rotation_only')
        self.assertTrue(result['cash_gate']['micro_orders_blocked'])
        self.assertEqual(result['position_scores'][0]['action'], 'EXIT')
        self.assertEqual(result['replacement_plan'][0]['action'], 'REPLACE_WITH')

    def test_sec_filings_create_structured_veto_events(self):
        cls = classify_filing('424B5')
        event = normalize_filing('ABC', {'form': '424B5', 'filingDate': '2026-06-10', 'accessionNumber': '1'})
        self.assertEqual(cls['event_type'], 'offering_dilution')
        self.assertEqual(event['event_type'], 'offering_dilution')
        self.assertLess(event['sentiment'], 0)

    def test_etf_generic_commentary_does_not_overboost_sector_etf(self):
        now = dt.datetime(2026, 6, 11, tzinfo=dt.timezone.utc)
        generic = NewsItem(symbol='XLF', headline='Stock market today: Wall Street futures mixed', source='finnhub', published_at=now, sentiment=0.6, materiality=0.9)
        sector = NewsItem(symbol='XLF', headline='Regional banks rally as yield curve steepens', source='finnhub', published_at=now, sentiment=0.6, materiality=0.9)
        generic_score = CatalystAgent([generic], now=now).score_symbol('XLF')
        sector_score = CatalystAgent([sector], now=now).score_symbol('XLF')
        self.assertLess(generic_score['score'], sector_score['score'])

    def test_performance_attribution_groups_by_setup_catalyst_sector(self):
        result = attribute_closed_trades([
            {'setup': 'pullback_ema21', 'catalyst_status': 'positive_catalyst', 'sector': 'Tech', 'pnl': 20, 'realized_r': 1.2},
            {'setup': 'pullback_ema21', 'catalyst_status': 'negative_news_veto', 'sector': 'Tech', 'pnl': -5, 'realized_r': -0.3},
        ])
        buckets = {row['bucket']: row for row in result['buckets']}
        self.assertEqual(buckets['setup:pullback_ema21']['trades'], 2)
        self.assertEqual(buckets['sector:Tech']['total_pnl_usd'], 15)

    def test_launch_profile_caps_first_cycle_but_not_existing_portfolio(self):
        proposals = []
        for idx, symbol in enumerate(['NVDA', 'XLF', 'XLE', 'MSFT', 'AMD']):
            proposals.append({
                'symbol': symbol,
                'confidence': 8 - idx * 0.1,
                'risk_reward': 2.0,
                'catalyst_score': 8,
                'order_intent': {'qty': 20, 'limit_price': 100},
            })
        policy = {
            'autonomous_mode': True,
            'risk_mode': {'max_total_exposure_pct': 85},
            'portfolio_construction': {
                'max_new_orders': 5,
                'max_sector_exposure_pct': 100,
                'launch_profile': {
                    'enabled': True,
                    'max_new_orders_first_cycle': 3,
                    'max_total_exposure_pct_first_cycle': 80,
                },
            },
        }
        first = construct_portfolio(proposals, {'cash': 10000, 'portfolio_value': 10000, 'positions': []}, policy)
        self.assertTrue(first['launch_profile']['applied'])
        self.assertLessEqual(first['selected_count'], 3)
        self.assertLessEqual(sum(p['requested_notional_usd'] for p in first['selected_proposals']), 8000)

        after_start = construct_portfolio(proposals, {'cash': 10000, 'portfolio_value': 10000, 'positions': [{'symbol': 'XLF'}]}, policy)
        self.assertFalse(after_start['launch_profile']['applied'])

    def test_policy_is_warm_started_but_not_authorized_to_trade(self):
        policy = json.loads(pathlib.Path('config/policy.json').read_text())
        self.assertEqual(policy['risk_mode']['min_confidence'], 4.5)
        self.assertEqual(policy['risk_mode']['min_risk_reward'], 1.3)
        self.assertFalse(policy['execution_authorization']['authorized_by_user'])
        self.assertFalse(policy['execution_authorization']['alpaca_paper_orders_after_full_pipeline'])
        self.assertEqual(policy['setup_rotation']['stats'], [])
        self.assertEqual(policy['fair_competition_readiness']['activation_state'], 'prepared_not_trading')
        self.assertEqual(policy['fair_competition_readiness']['expected_cron_offset']['v2_minutes'], [7, 22, 37, 52])


if __name__ == '__main__':
    unittest.main()
