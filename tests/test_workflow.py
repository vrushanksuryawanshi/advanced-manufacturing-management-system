import concurrent.futures
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import secrets
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app, seed_demo, stamp, utcnow, dt


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        Path('tmp/tests').mkdir(parents=True, exist_ok=True)
        self.path = Path('tmp/tests') / (secrets.token_hex(8)+'.db')
        self.app = create_app(self.path, testing=True)
        seed_demo(self.app)
        self.manager, self.mt = self.login('manager')
        self.operator, self.ot = self.login('operator')
        self.client, self.ct = self.login('client')
        self.station_staff = {1:(self.operator,self.ot)}
        for station,kind in [(2,'production'),(3,'production'),(4,'qc'),(5,'storage')]:
            result=self.post('/users',dict(name=f'Staff {station}',email=f'staff{station}@demo.local',password='FactoryDemo123!',role='operator',worker_type=kind,machine_id=station))
            self.assertEqual(result.status_code,201,result.json)
            self.station_staff[station]=self.login(f'staff{station}')

    def login(self, role):
        client = self.app.test_client()
        token = client.get('/api/session').json['csrf']
        res = client.post('/api/login', json={'email':role+'@demo.local','password':'FactoryDemo123!'}, headers={'X-CSRF-Token':token})
        self.assertEqual(res.status_code,200)
        return client,res.json['csrf']

    def post(self, path, data, operator=False):
        client,token = (self.operator,self.ot) if operator else (self.manager,self.mt)
        if path in ('/orders','/routes') and 'warehouse_slot_id' not in data:
            slot=next(s for s in self.state()['warehouse_slots'] if s['status']=='available')
            data={**data,'warehouse_slot_id':slot['id']}
        return client.post('/api'+path,json=data,headers={'X-CSRF-Token':token})

    def state(self):
        return self.manager.get('/api/state').json

    def first_lot(self):
        return min(self.state()['orders'],key=lambda o:o['id'])['lots'][0]

    def action(self, op, action, **kwargs):
        client,token=self.station_staff[op['machine_id']]
        return client.post(f'/api/operations/{op["id"]}/action',json=dict(action=action,**kwargs),headers={'X-CSRF-Token':token})

    def test_end_to_end_route_and_storage(self):
        lot=self.first_lot()
        for op in lot['operations']:
            self.assertEqual(self.action(op,'start').status_code,200)
            self.assertEqual(self.action(op,'complete',result='pass',location='Rack A-12').status_code,200)
        result=self.operator.get('/api/scan',query_string={'code':lot['code']}).json['lot']
        self.assertEqual(result['status'],'finished')
        self.assertEqual(result['progress'],100)
        self.assertEqual(result['location'],'1/1')
        slot=next(s for s in self.state()['warehouse_slots'] if s['id']==1)
        self.assertEqual(slot['status'],'occupied')
        wh,token=self.station_staff[5]
        self.assertEqual(wh.post('/api/warehouse-slots/1/dispatch',json={'reference':'SHIP-001'},headers={'X-CSRF-Token':token}).status_code,200)
        self.assertEqual(next(s for s in self.state()['warehouse_slots'] if s['id']==1)['status'],'available')

    def test_no_skipping_and_no_duplicate_start(self):
        ops=self.first_lot()['operations']
        self.assertEqual(self.action(ops[1],'start').status_code,409)
        self.assertEqual(self.action(ops[0],'start').status_code,200)
        self.assertEqual(self.action(ops[0],'start').status_code,409)
        self.assertEqual(self.action(ops[0],'complete').status_code,200)
        self.assertEqual(self.action(ops[0],'complete').status_code,409)

    def test_concurrent_start_only_one_succeeds(self):
        op=self.first_lot()['operations'][0]
        def start(_):
            client,token=self.login('operator')
            return client.post(f'/api/operations/{op["id"]}/action',json={'action':'start'},headers={'X-CSRF-Token':token}).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(start,range(2))),[200,409])

    def test_client_isolation_and_no_internal_details(self):
        data=self.client.get('/api/state').json
        self.assertEqual(len(data['orders']),2)
        self.assertNotIn('users',data)
        for order in data['orders']:
            for private in ['lots','eta','client_id','route_id','operations']:
                self.assertNotIn(private,order)
        self.assertEqual(self.client.get('/api/scan?code=x').status_code,403)
        self.assertEqual(self.client.get('/api/lots/1/qr').status_code,403)

    def test_role_and_csrf_guards(self):
        self.assertEqual(self.operator.post('/api/orders',json={},headers={'X-CSRF-Token':self.ot}).status_code,403)
        self.assertEqual(self.manager.post('/api/parts',json={'code':'x','name':'x'}).status_code,403)
        self.assertEqual(self.app.test_client().get('/api/state').status_code,401)

    def test_operator_station_assignment(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM assignments WHERE user_id=2 AND machine_id=1')
        self.assertEqual(self.action(self.first_lot()['operations'][0],'start').status_code,403)

    def test_unique_qr_after_order_route_confirmation(self):
        res=self.post('/orders',dict(client_id=3,route_id=1,quantity=101,lot_size=50,priority=2,due_date='2026-12-31'))
        self.assertEqual(res.status_code,201)
        order=next(o for o in self.state()['orders'] if o['id']==res.json['id'])
        self.assertEqual([l['quantity'] for l in order['lots']],[50,50,1])
        self.assertEqual(len(set(l['token'] for l in order['lots'])),3)
        for lot in order['lots']:
            qr=self.manager.get(f'/api/lots/{lot["id"]}/qr')
            self.assertEqual(qr.status_code,200)
            self.assertIn(b'<svg',qr.data)
            scan=self.operator.get('/api/scan',query_string={'code':'AMMS:'+lot['token']})
            self.assertEqual(scan.json['lot']['id'],lot['id'])

    def test_new_route_keeps_old_order_snapshot(self):
        old=self.first_lot()['operations']
        route=self.state()['routes'][-1]
        steps=route['steps']
        steps[0]['minutes']=99
        res=self.post('/routes',dict(part_id=route['part_id'],steps=steps))
        self.assertEqual(res.status_code,201)
        self.assertEqual(res.json['version'],2)
        self.assertEqual(self.first_lot()['operations'][0]['minutes'],old[0]['minutes'])

    def test_invalid_route_rollback(self):
        before=len(self.state()['routes'])
        result=self.post('/routes',dict(part_id=1,steps=[dict(machine_id=1,minutes=10,transition_minutes=0)]*3))
        self.assertEqual(result.status_code,400)
        self.assertEqual(len(self.state()['routes']),before)

    def test_machine_schedule_has_no_overlap_and_respects_precedence(self):
        operations=[op for o in self.state()['orders'] for l in o['lots'] for op in l['operations']]
        for machine in range(1,6):
            ops=sorted([o for o in operations if o['machine_id']==machine],key=lambda o:o['forecast_start'])
            for previous,current in zip(ops,ops[1:]):
                self.assertGreaterEqual(dt(current['forecast_start']),dt(previous['forecast_end']))
        for order in self.state()['orders']:
            for lot in order['lots']:
                for previous,current in zip(lot['operations'],lot['operations'][1:]):
                    self.assertGreaterEqual(dt(current['forecast_start']),dt(previous['forecast_end'])+timedelta(minutes=current['transition_minutes']))

    def test_override_audit_and_baseline_preservation(self):
        order=max(self.state()['orders'],key=lambda o:o['id'])
        op=order['lots'][0]['operations'][1]
        baseline=op['planned_start']
        self.assertEqual(self.post(f'/operations/{op["id"]}/prioritize',{'reason':''}).status_code,400)
        self.assertEqual(self.post(f'/operations/{op["id"]}/prioritize',{'reason':'Urgent client need'}).status_code,200)
        updated=next(o for o in self.state()['orders'] if o['id']==order['id'])['lots'][0]['operations'][1]
        self.assertEqual(updated['planned_start'],baseline)
        self.assertLess(updated['forecast_start'],op['forecast_start'])
        self.assertEqual(self.state()['events'][0]['kind'],'Queue priority override')

    def test_active_operation_cannot_be_preempted(self):
        first=self.first_lot()['operations'][0]
        self.assertEqual(self.action(first,'start').status_code,200)
        other=max(self.state()['orders'],key=lambda o:o['id'])['lots'][0]['operations'][0]
        self.assertEqual(self.post(f'/operations/{other["id"]}/prioritize',{'reason':'Rush job'}).status_code,200)
        self.assertEqual(self.action(other,'start').status_code,409)
        self.assertEqual(self.post(f'/operations/{first["id"]}/prioritize',{'reason':'Rush job'}).status_code,409)

    def test_qc_hold_reinspection_and_storage_validation(self):
        ops=self.first_lot()['operations']
        for op in ops[:3]:
            self.action(op,'start');self.action(op,'complete')
        qc=ops[3]
        self.assertEqual(self.action(qc,'start').status_code,200)
        self.assertEqual(self.action(qc,'complete',result='fail').status_code,400)
        self.assertEqual(self.action(qc,'complete',result='fail',notes='Diameter out of tolerance').status_code,200)
        self.assertEqual(self.first_lot()['status'],'hold')
        self.assertIsNone(self.first_lot()['eta'])
        self.assertEqual(self.action(ops[4],'start').status_code,409)
        self.assertEqual(self.post(f'/operations/{qc["id"]}/release',{'reason':'Correction verified; inspect again'}).status_code,200)
        self.assertEqual(self.action(qc,'start').status_code,200)
        self.assertEqual(self.action(qc,'complete',result='pass').status_code,200)
        self.assertEqual(self.action(ops[4],'start').status_code,200)
        self.assertEqual(self.action(ops[4],'complete').status_code,200)
        self.assertEqual(self.first_lot()['location'],'1/1')
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM quality').fetchone()[0],2)

    def test_unavailable_machine_blocks_downstream_eta(self):
        res=self.post('/machines/2/availability',dict(available=False,reason='Tool maintenance'))
        self.assertEqual(res.status_code,200)
        self.assertIsNone(self.first_lot()['eta'])
        self.assertEqual(self.post('/machines/2/availability',dict(available=True,reason='Ready')).status_code,200)
        self.assertIsNotNone(self.first_lot()['eta'])

    def test_client_eta_requires_manager_approval(self):
        order=min(self.state()['orders'],key=lambda o:o['id'])
        self.assertIsNone(next(o for o in self.client.get('/api/state').json['orders'] if o['id']==order['id'])['approved_eta'])
        self.assertEqual(self.post(f'/orders/{order["id"]}/approve-eta',{}).status_code,200)
        self.assertIsNotNone(next(o for o in self.client.get('/api/state').json['orders'] if o['id']==order['id'])['approved_eta'])

    def test_benchmark_requires_five_prior_comparable_records(self):
        lot=self.first_lot()
        now=utcnow()
        with closing(sqlite3.connect(self.path)) as db, db:
            for i in range(5):
                cur=db.execute('INSERT INTO lots(order_id,code,token,quantity) VALUES(?,?,?,?)',(lot['order_id'],f'HISTORY-{i}',secrets.token_hex(24),lot['quantity']))
                db.execute("INSERT INTO operations(lot_id,seq,machine_id,minutes,transition_minutes,instructions,status,actual_start,actual_end) VALUES(?,1,1,30,0,'','complete',?,?)",(cur.lastrowid,stamp(now-timedelta(days=i+1,minutes=40)),stamp(now-timedelta(days=i+1))))
        result=self.first_lot()['operations'][0]
        self.assertEqual(result['sample_count'],5)
        self.assertAlmostEqual(result['historical_minutes'],40)

    def test_validation_rejects_nonfinite_and_fractional_quantities(self):
        base=dict(client_id=3,route_id=1,quantity=10,lot_size=10,priority=2,due_date='2026-12-31')
        for quantity in [0,-1,1.2,'nan','inf','x']:
            self.assertEqual(self.post('/orders',{**base,'quantity':quantity}).status_code,400)

    def test_password_hash_and_failed_login(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            password=db.execute('SELECT password FROM users LIMIT 1').fetchone()[0]
            self.assertNotEqual(password,'FactoryDemo123!')
        self.assertEqual(self.manager.post('/api/login',json={'email':'manager@demo.local','password':'wrong'},headers={'X-CSRF-Token':self.mt}).status_code,401)


if __name__=='__main__':
    unittest.main(verbosity=2)
