import concurrent.futures
import secrets
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import PostgresConnection,create_app,seed_demo


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

    def test_manager_can_correct_worker_role_and_station(self):
        result=self.manager.put('/api/operators/2/assignment',json=dict(worker_type='qc',machine_id=4),headers={'X-CSRF-Token':self.token})
        self.assertEqual(result.status_code,200,result.json)
        updated=next(u for u in self.state()['users'] if u['id']==2)
        self.assertEqual(updated['worker_type'],'qc')
        self.assertEqual(next(a['machine_id'] for a in self.state()['assignments'] if a['user_id']==2),4)
        self.assertEqual(self.state()['events'][0]['kind'],'Staff assignment changed')

        wrong=self.manager.put('/api/operators/2/assignment',json=dict(worker_type='storage',machine_id=4),headers={'X-CSRF-Token':self.token})
        self.assertEqual(wrong.status_code,400,wrong.json)
        self.assertEqual(next(a['machine_id'] for a in self.state()['assignments'] if a['user_id']==2),4)

    def test_worker_with_active_operation_cannot_be_reassigned(self):
        with sqlite3.connect(self.path) as database:
            operation_id=database.execute('SELECT id FROM operations WHERE machine_id=1 LIMIT 1').fetchone()[0]
            database.execute("UPDATE operations SET status='running',operator_id=2,actual_start='2026-10-21T10:00:00+00:00' WHERE id=?",(operation_id,))
        result=self.manager.put('/api/operators/2/assignment',json=dict(worker_type='qc',machine_id=4),headers={'X-CSRF-Token':self.token})
        self.assertEqual(result.status_code,409,result.json)
        self.assertIn('active operation',result.json['error'])
        self.assertEqual(next(a['machine_id'] for a in self.state()['assignments'] if a['user_id']==2),1)

    def test_manager_can_reset_staff_and_client_passwords(self):
        self.assertEqual(self.manager.put('/api/users/2/password',json={'password':'short'},headers={'X-CSRF-Token':self.token}).status_code,400)
        self.assertEqual(self.manager.put('/api/users/1/password',json={'password':'ReplacementPassword123!'},headers={'X-CSRF-Token':self.token}).status_code,400)

        for user_id,email in [(2,'operator@demo.local'),(3,'client@demo.local')]:
            new_password=f'ReplacementPassword{user_id}!'
            result=self.manager.put(f'/api/users/{user_id}/password',json={'password':new_password},headers={'X-CSRF-Token':self.token})
            self.assertEqual(result.status_code,200,result.json)
            account=self.app.test_client();csrf=account.get('/api/session').json['csrf']
            old=account.post('/api/login',json={'email':email,'password':'FactoryDemo123!'},headers={'X-CSRF-Token':csrf})
            self.assertEqual(old.status_code,401)
            new=account.post('/api/login',json={'email':email,'password':new_password},headers={'X-CSRF-Token':csrf})
            self.assertEqual(new.status_code,200,new.json)

        self.assertEqual(self.state()['events'][0]['kind'],'Account password changed')

    def test_staff_cannot_reset_an_account_password(self):
        staff=self.app.test_client();csrf=staff.get('/api/session').json['csrf']
        csrf=staff.post('/api/login',json={'email':'operator@demo.local','password':'FactoryDemo123!'},headers={'X-CSRF-Token':csrf}).json['csrf']
        result=staff.put('/api/users/3/password',json={'password':'ReplacementPassword123!'},headers={'X-CSRF-Token':csrf})
        self.assertEqual(result.status_code,403,result.json)

    def test_machine_inventory_and_warehouse_extension(self):
        self.assertEqual(sum(m['kind']=='production' for m in self.state()['machines']),12)
        self.assertEqual(self.post('/machines',dict(code='M27',name='New mill',kind='production')).status_code,201)
        self.assertEqual(self.post('/warehouses/5/rows',dict(bays=10)).status_code,201)
        self.assertIn('4/10',[s['address'] for s in self.state()['warehouse_slots']])

    def test_manager_can_delete_only_unused_machine(self):
        machine_id=self.post('/machines',dict(code='TEMP',name='Created by mistake',kind='production')).json['id']
        result=self.manager.delete(f'/api/machines/{machine_id}',json={},headers={'X-CSRF-Token':self.token})
        self.assertEqual(result.status_code,200,result.json)
        self.assertFalse(any(m['id']==machine_id for m in self.state()['machines']))
        self.assertEqual(self.state()['events'][0]['kind'],'Machine removed')

        result=self.manager.delete('/api/machines/1',json={},headers={'X-CSRF-Token':self.token})
        self.assertEqual(result.status_code,409,result.json)
        self.assertIn('saved product route',result.json['error'])
        self.assertTrue(any(m['id']==1 for m in self.state()['machines']))

    def test_unused_warehouse_removal_cleans_generated_addresses(self):
        warehouse_id=self.post('/machines',dict(code='TEMP-WH',name='Temporary warehouse',kind='storage')).json['id']
        self.assertEqual(sum(s['machine_id']==warehouse_id for s in self.state()['warehouse_slots']),30)
        result=self.manager.delete(f'/api/machines/{warehouse_id}',json={},headers={'X-CSRF-Token':self.token})
        self.assertEqual(result.status_code,200,result.json)
        self.assertFalse(any(s['machine_id']==warehouse_id for s in self.state()['warehouse_slots']))

    def test_manager_can_delete_only_unused_warehouse_address(self):
        warehouse_id=self.post('/machines',dict(code='SLOT-WH',name='Slot test warehouse',kind='storage')).json['id']
        slot=next(s for s in self.state()['warehouse_slots'] if s['machine_id']==warehouse_id)
        result=self.manager.delete(f"/api/warehouse-slots/{slot['id']}",json={},headers={'X-CSRF-Token':self.token})
        self.assertEqual(result.status_code,200,result.json)
        self.assertFalse(any(s['id']==slot['id'] for s in self.state()['warehouse_slots']))
        self.assertEqual(self.state()['events'][0]['kind'],'Warehouse address removed')

        result=self.manager.delete('/api/warehouse-slots/1',json={},headers={'X-CSRF-Token':self.token})
        self.assertEqual(result.status_code,409,result.json)
        self.assertTrue(any(s['id']==1 for s in self.state()['warehouse_slots']))

    def test_removed_client_email_can_be_added_again(self):
        self.app.config.update(SMTP_HOST='smtp.test',SMTP_FROM='factory@example.com')
        data=dict(name='Temporary Client',email='temporary.client@example.com',role='client')
        with patch('app.smtplib.SMTP'):
            created=self.post('/users',data)
            self.assertEqual(created.status_code,201,created.json)
            removed=self.manager.delete(f"/api/users/{created.json['id']}",json={},headers={'X-CSRF-Token':self.token})
            self.assertEqual(removed.status_code,200,removed.json)
            self.assertFalse(any(u['email']==data['email'] for u in self.state()['users']))
            recreated=self.post('/users',data)
            self.assertEqual(recreated.status_code,201,recreated.json)

        result=self.manager.delete('/api/users/3',json={},headers={'X-CSRF-Token':self.token})
        self.assertEqual(result.status_code,409,result.json)
        self.assertIn('production orders',result.json['error'])

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

    def test_route_and_order_can_reserve_multiple_warehouse_addresses(self):
        original=next(r for r in self.state()['routes'] if r['id']==1)
        route=self.post('/routes',dict(part_id=original['part_id'],steps=original['steps'],warehouse_slot_ids=[10,11]))
        self.assertEqual(route.status_code,201,route.json)
        saved=next(r for r in self.state()['routes'] if r['id']==route.json['id'])
        self.assertEqual(saved['warehouse_slot_ids'],[10,11])

        order=self.post('/orders',dict(client_id=3,route_id=route.json['id'],quantity=20,lot_size=10,priority=2,due_date='2027-01-01',warehouse_slot_ids=[10,11]))
        self.assertEqual(order.status_code,201,order.json)
        saved_order=next(o for o in self.state()['orders'] if o['id']==order.json['id'])
        self.assertEqual(saved_order['warehouse_slot_ids'],[10,11])
        reserved=[s for s in self.state()['warehouse_slots'] if s['order_id']==order.json['id']]
        self.assertEqual({s['id'] for s in reserved},{10,11})

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

    def test_postgres_bulk_insert_uses_cursor(self):
        calls=[]
        class Cursor:
            def executemany(self,sql,args):
                calls.append((sql,args))
        class Connection:
            def cursor(self):
                return Cursor()
        adapter=PostgresConnection.__new__(PostgresConnection)
        adapter.connection=Connection()
        result=adapter.executemany('INSERT INTO example(a,b) VALUES(?,?)',[(1,2),(3,4)])
        self.assertEqual(calls,[('INSERT INTO example(a,b) VALUES(%s,%s)',[(1,2),(3,4)])])
        self.assertIsInstance(result.cursor,Cursor)


if __name__=='__main__':
    unittest.main()
