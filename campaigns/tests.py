"""Сценарии из видео-ТЗ на фейковом Keitaro в памяти. Запуск: python manage.py test"""
from django.test import TestCase

from . import services
from .models import Campaign, OfferLink
from .services import EditorError
from .shares import split_shares

OFFERS = {3749: 'Miaflow spin', 3717: 'Miaflow prize', 11111: 'FitoMishki', 555: 'Extra'}


class FakeKeitaro:
    def __init__(self):
        self.streams_by_campaign = {1: [
            {'id': 10, 'name': 'Flow 1', 'position': 1, 'offers': [],
             'filters': [{'name': 'country', 'mode': 'accept', 'payload': ['AU']}]},
            {'id': 20, 'name': 'Flow 2', 'position': 2, 'offers': [
                {'offer_id': 3749, 'share': 33}, {'offer_id': 3717, 'share': 33}, {'offer_id': 11111, 'share': 34}]},
        ]}
        self.created = []

    def offers(self):
        return [{'id': i, 'name': n, 'state': 'active'} for i, n in OFFERS.items()]

    def streams(self, campaign_id):
        return self.streams_by_campaign[campaign_id]

    def campaign(self, campaign_id):
        return {'id': campaign_id, 'name': 'campaign 2'}

    def set_stream_offers(self, stream_id, offers):
        for streams in self.streams_by_campaign.values():
            for s in streams:
                if s['id'] == stream_id:
                    s['offers'] = [{'offer_id': o['offer_id'], 'share': o['share']} for o in offers]

    def create_campaign(self, **fields):
        self.created.append(('campaign', fields))
        self.streams_by_campaign[2] = []
        return {'id': 2, 'name': fields['name']}

    def create_stream(self, **fields):
        self.created.append(('stream', fields))
        self.streams_by_campaign[fields['campaign_id']].append(
            {'id': 100 + len(self.created), 'name': fields['name'], 'position': fields['position'],
             'offers': [{'offer_id': o['offer_id'], 'share': o['share']} for o in fields.get('offers', [])]})


def shares(stream):
    return {link.offer_id: link.share for link in stream.links.all()}


class SharesTest(TestCase):
    def test_split(self):
        self.assertEqual(split_shares(3), [33, 33, 34])
        self.assertEqual(split_shares(4), [25, 25, 25, 25])
        self.assertEqual(split_shares(3, 75), [25, 25, 25])
        self.assertEqual(split_shares(0), [])
        self.assertTrue(all(sum(split_shares(n)) == 100 for n in range(1, 101)))


class EditorTest(TestCase):
    def setUp(self):
        self.kt = FakeKeitaro()
        self.campaign = services.open_campaign(1, self.kt)
        self.flow1, self.flow2 = self.campaign.streams.all()

    def link(self, offer_id):
        return self.flow2.links.get(offer_id=offer_id)

    def test_fetch_keeps_offerless_stream_unparsed(self):
        self.assertFalse(self.flow1.has_offers)
        self.assertEqual(self.flow1.links.count(), 0)
        self.assertEqual(shares(self.flow2), {3749: 33, 3717: 33, 11111: 34})
        self.assertFalse(self.flow2.is_dirty)

    def test_add_recalcs_and_waits_for_push(self):
        services.add_offer(self.flow2, 555, self.kt)
        self.assertEqual(shares(self.flow2), {3749: 25, 3717: 25, 11111: 25, 555: 25})
        self.assertTrue(self.flow2.is_dirty)
        self.assertEqual(len(self.kt.streams(1)[1]['offers']), 3)  # в Keitaro пока без изменений
        services.push(self.flow2, self.kt)
        self.assertFalse(self.flow2.is_dirty)
        self.assertEqual({o['offer_id']: o['share'] for o in self.kt.streams(1)[1]['offers']},
                         {3749: 25, 3717: 25, 11111: 25, 555: 25})

    def test_cancel_drops_unpublished_offer(self):
        services.add_offer(self.flow2, 555, self.kt)
        services.cancel(self.flow2)
        self.assertEqual(shares(self.flow2), {3749: 33, 3717: 33, 11111: 34})
        self.assertFalse(self.flow2.is_dirty)

    def test_remove_archives_and_bring_back(self):
        services.remove_offer(self.link(11111))
        self.assertEqual(shares(self.flow2), {3749: 50, 3717: 50, 11111: 0})
        self.assertEqual(self.link(11111).state, OfferLink.REMOVING)
        services.push(self.flow2, self.kt)
        self.assertNotIn(11111, [o['offer_id'] for o in self.kt.streams(1)[1]['offers']])
        self.assertEqual(self.link(11111).state, OfferLink.REMOVED)  # остался у нас в архиве
        services.restore_offer(self.link(11111))
        self.assertTrue(self.flow2.is_dirty)
        services.push(self.flow2, self.kt)
        self.assertEqual(shares(self.flow2), {3749: 33, 3717: 33, 11111: 34})

    def test_offer_deleted_in_keitaro_goes_to_archive(self):
        self.kt.streams(1)[1]['offers'] = [{'offer_id': 3749, 'share': 50}, {'offer_id': 3717, 'share': 50}]
        services.fetch_streams(self.campaign, self.kt)
        self.assertEqual(self.link(11111).state, OfferLink.REMOVED)
        self.assertEqual(shares(self.flow2), {3749: 50, 3717: 50, 11111: 0})

    def test_pinned_share_survives_recalc(self):
        services.pin_offer(self.link(3749), 25)
        self.assertEqual(shares(self.flow2), {3749: 25, 3717: 37, 11111: 38})
        services.add_offer(self.flow2, 555, self.kt)
        self.assertEqual(shares(self.flow2), {3749: 25, 3717: 25, 11111: 25, 555: 25})
        services.remove_offer(self.link(11111))
        self.assertEqual(shares(self.flow2), {3749: 25, 3717: 37, 11111: 0, 555: 38})

    def test_pins_over_100_rejected(self):
        services.pin_offer(self.link(3749), 60)
        with self.assertRaises(EditorError):
            services.pin_offer(self.link(3717), 50)
        self.assertFalse(self.link(3717).pinned)  # транзакция откатилась

    def test_push_requires_100(self):
        services.pin_offer(self.link(3749), 30)
        services.pin_offer(self.link(3717), 30)
        services.remove_offer(self.link(11111))
        with self.assertRaises(EditorError):
            services.push(self.flow2, self.kt)


class ClientTest(TestCase):
    def test_network_errors_retry_then_become_keitaro_error(self):
        from unittest import mock

        import requests

        from .keitaro import Keitaro, KeitaroError
        kt = Keitaro('https://kt.example', 'key')
        ok = mock.Mock(status_code=200, content=b'[]', json=lambda: [])
        with mock.patch('campaigns.keitaro.time.sleep'), \
                mock.patch.object(kt.session, 'request', side_effect=[requests.ConnectionError(), ok]) as req:
            self.assertEqual(kt.domains(), [])  # GET повторился и прошёл
            self.assertEqual(req.call_count, 2)
        with mock.patch.object(kt.session, 'request', side_effect=requests.ConnectionError()) as req:
            with self.assertRaises(KeitaroError):
                kt.create_campaign(name='x', alias='x')
            self.assertEqual(req.call_count, 1)  # POST не повторяем


class CreatorTest(TestCase):
    def test_parse_geo(self):
        self.assertEqual(services.parse_geo('mx, au;RO au'), ['MX', 'AU', 'RO'])
        with self.assertRaises(EditorError):
            services.parse_geo('Australia')

    def test_creates_two_flows(self):
        kt = FakeKeitaro()
        campaign = services.create_campaign(kt, name='test', geo=['AU'], offer_id=555, group_id=7)
        kinds = [k for k, _ in kt.created]
        self.assertEqual(kinds, ['campaign', 'stream', 'stream'])
        self.assertEqual(kt.created[0][1]['group_id'], 7)
        geo_flow, offer_flow = kt.created[1][1], kt.created[2][1]
        self.assertEqual(geo_flow['filters'], [{'name': 'country', 'mode': 'accept', 'payload': ['AU']}])
        self.assertEqual(geo_flow['action_payload'], services.GOOGLE_URL)
        self.assertEqual(offer_flow['offers'], [{'offer_id': 555, 'share': 100, 'state': 'active'}])
        self.assertEqual(Campaign.objects.get(kt_id=2), campaign)
        self.assertEqual(campaign.streams.count(), 2)
