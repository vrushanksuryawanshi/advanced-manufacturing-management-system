import concurrent.futures
import secrets
import sqlite3
import unittest
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app,seed_demo


class FactoryFloorTests(unittest.TestCase):
    def setUp(self):
        Path('tmp/tests').mkdir(parents=True,exist_ok=True)
        self.path=Path('tmp/tests')/('floor-'+secrets.token_hex(6)+'.db')
        self.app=create_app(self.path,True)
        seed_demo(self.app)
        self.manager=self.app.test_client()
        token=self.manager.get('/api/session').json['csrf']
        result=self.manager.post('/api/login',json={'email':'manager@demo.local','password':'FactoryDemo123!'},headers={'X-CSRF-Token':token})
        self.token=result.json['csrf']

    def post(self,path,data):
        return self.manager.post('/api'+path,json=data,headers={'X-CSRF-Token':self.token})

    def state(self):
        return self.manager.get('/api/state').json

    def test_job_registration_approval_and_station_filter(self):
        for kind,station in [('production',1),('qc',4),('storage',5)]:
            staff=self.app.test_client();token=staff.get('/api/session').json['csrf']
            choices=staff.get('/api/registration-machines?worker_type='+kind).json['machines']
            self.assertTrue(choices)
            self.assertTrue(all(m['kind']==kind for m in choices))
            data=dict(name='New staff',email=kind+'@example.com',password='RegisterPassword123!',worker_type=kind,machine_id=station)
            result=staff.post('/api/operator-register',json=data,headers={'X-CSRF-Token':token})
            self.assertEqual(result.status_code,201,result.json)
            login=lambda:staff.post('/api/login',json=data,headers={'X-CSRF-Token':token})
            self.assertEqual(login().status_code,403)
            pending=next(u for u in self.state()['users'] if u['email']==data['email'])
            wrong=4 if kind=='production' else 1
            self.assertEqual(self.post(f"/operators/{pending['id']}/approve",dict(machine_id=wrong)).status_code,400)
            self.assertEqual(self.post(f"/operators/{pending['id']}/approve",dict(machine_id=station)).status_code,200)
            result=login();self.assertEqual(result.status_code,200,result.json)
            self.assertEqual(result.json['user']['worker_type'],kind)
            self.assertEqual(staff.get('/api/state').json['assignments'],[station])

    def test_production_operator_cannot_request_qc(self):
        c=self.app.test_client();token=c.get('/api/session').json['csrf']
        res=c.post('/api/operator-register',json=dict(name='Wrong job',email='wrong@example.com',password='RegisterPassword123!',worker_type='production',machine_id=4),headers={'X-CSRF-Token':token})
        self.assertEqual(res.status_code,400)
        self.assertFalse(any(u['email']=='wrong@example.com' for u in self.state()['users']))

    def test_machine_inventory_and_warehouse_extension(self):
        self.assertEqual(sum(m['kind']=='production' for m in self.state()['machines']),12)
        self.assertEqual(self.post('/machines',dict(code='M27',name='New mill',kind='production')).status_code,201)
        self.assertEqual(self.post('/warehouses/5/rows',dict(bays=10)).status_code,201)
        self.assertIn('4/10',[s['address'] for s in self.state()['warehouse_slots']])

    def test_duplicate_reservation_rejected_and_order_rolled_back(self):
        count=len(self.state()['orders'])
        res=self.post('/orders',dict(client_id=3,route_id=1,quantity=10,lot_size=10,priority=2,due_date='2027-01-01',warehouse_slot_id=1))
        self.assertEqual(res.status_code,409)
        self.assertEqual(len(self.state()['orders']),count)

    def test_simultaneous_orders_cannot_reserve_same_address(self):
        def create(_):
            c=self.app.test_client();token=c.get('/api/session').json['csrf']
            token=c.post('/api/login',json=dict(email='manager@demo.local',password='FactoryDemo123!'),headers={'X-CSRF-Token':token}).json['csrf']
            return c.post('/api/orders',json=dict(client_id=3,route_id=1,quantity=10,lot_size=10,priority=2,due_date='2027-01-01',warehouse_slot_id=10),headers={'X-CSRF-Token':token}).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(create,range(2))),[201,409])

    def test_wrong_warehouse_slot_rejected(self):
        warehouse=self.post('/machines',dict(code='WH2',name='Second warehouse',kind='storage')).json['id']
        slot=next(s for s in self.state()['warehouse_slots'] if s['machine_id']==warehouse)
        res=self.post('/orders',dict(client_id=3,route_id=1,quantity=10,lot_size=10,priority=2,due_date='2027-01-01',warehouse_slot_id=slot['id']))
        self.assertEqual(res.status_code,400)

    def test_station_kind_enforced_even_if_assignment_is_corrupt(self):
        c=self.app.test_client();token=c.get('/api/session').json['csrf']
        token=c.post('/api/login',json=dict(email='operator@demo.local',password='FactoryDemo123!'),headers={'X-CSRF-Token':token}).json['csrf']
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE assignments SET machine_id=4 WHERE user_id=2')
        op=self.state()['orders'][0]['lots'][0]['operations'][3]
        res=c.post(f"/api/operations/{op['id']}/action",json={'action':'start'},headers={'X-CSRF-Token':token})
        self.assertEqual(res.status_code,403)

    def test_fresh_and_existing_database_startup(self):
        again=create_app(self.path,True)
        self.assertEqual(again.test_client().get('/api/registration-machines').status_code,200)
        self.assertEqual(len(self.state()['warehouse_slots']),30)


if __name__=='__main__':
    unittest.main()
