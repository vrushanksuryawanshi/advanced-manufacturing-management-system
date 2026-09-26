const {test,expect}=require('@playwright/test');
const fs=require('fs');
async function login(page,role){
  await page.goto('/');
  await page.getByLabel('Email address',{exact:true}).fill(`${role}@demo.local`);
  await page.getByLabel('Password',{exact:true}).fill('FactoryDemo123!');
  await page.getByRole('button',{name:'Sign in to workspace'}).click();
  await expect(page.locator('.workspace')).toBeVisible();
}
async function noOverflow(page){expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);}

test('manager desktop and phone layouts, route versioning and automatic QR labels',async({page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setViewportSize({width:1440,height:1000});
  await login(page,'manager');
  await expect(page.getByRole('heading',{name:'Production overview'})).toBeVisible();
  await noOverflow(page);
  fs.mkdirSync('tmp/screenshots',{recursive:true});
  await page.screenshot({path:'tmp/screenshots/manager-desktop.png',fullPage:true});
  await page.getByRole('button',{name:'Factory floor',exact:true}).click();
  await expect(page.locator('.floor-station.production')).toHaveCount(12);
  await page.screenshot({path:'tmp/screenshots/factory-floor.png',fullPage:true});
  await page.getByRole('button',{name:'Products & routes',exact:true}).click();
  await page.getByRole('button',{name:'Open product'}).first().click();
  await page.getByRole('button',{name:'Revise route',exact:true}).click();
  await page.getByRole('button',{name:'Clear route',exact:true}).click();
  for(const code of ['M1','M3','M7','QC','WH']) await page.locator('#studio-floor .floor-station').filter({has:page.locator('strong',{hasText:new RegExp('^'+code+'$')})}).click();
  await page.getByRole('button',{name:'Address 1/4, available',exact:true}).click();
  await page.getByLabel('Minutes per lot',{exact:true}).first().fill('35');
  await page.screenshot({path:'tmp/screenshots/route-studio.png',fullPage:true});
  await page.getByRole('button',{name:'Save route version'}).click();
  await expect(page.locator('#view-route-version')).toBeVisible();
  await expect(page.locator('dialog .journey')).toContainText('M3');
  await expect(page.locator('dialog .journey')).toContainText('Address 1/4');
  await page.screenshot({path:'tmp/screenshots/product-route.png',fullPage:true});
  await page.getByRole('button',{name:'Close dialog'}).click();
  await expect(page.getByText('2 saved route versions')).toBeVisible();
  await page.getByRole('button',{name:'Production orders',exact:true}).click();
  await page.getByRole('button',{name:'New order',exact:true}).click();
  await page.getByLabel('Total quantity',{exact:true}).fill('12');
  await page.getByLabel('Units per lot',{exact:true}).fill('6');
  await page.getByRole('button',{name:'Confirm route & generate QR'}).click();
  await expect(page.getByRole('heading',{name:'Lot QR labels'})).toBeVisible();
  await expect(page.locator('.qr-label')).toHaveCount(2);
  await expect(page.locator('.qr-label img').first()).toBeVisible();
  expect(await page.locator('.qr-label img').first().evaluate(el=>el.complete&&el.naturalWidth>0)).toBe(true);
  await page.getByRole('button',{name:'Close dialog'}).click();
  await page.setViewportSize({width:390,height:844});
  await noOverflow(page);
  await page.screenshot({path:'tmp/screenshots/orders-mobile.png',fullPage:true});
  for(const label of ['Overview','Factory floor','Warehouse','Machine schedule','Products & routes','Machines','Activity log','Team & access']){
    await page.getByRole('button',{name:'Open navigation'}).click();
    await page.getByRole('button',{name:label,exact:true}).click();
    await noOverflow(page);
    if(label==='Factory floor')await page.screenshot({path:'tmp/screenshots/factory-floor-mobile.png',fullPage:true});
    if(label==='Warehouse')await page.screenshot({path:'tmp/screenshots/warehouse-mobile.png',fullPage:true});
    if(label==='Products & routes'){
      await page.getByRole('button',{name:'Open product'}).first().click();
      await expect(page.locator('dialog .journey')).toContainText('M3');
      await page.getByRole('button',{name:'Revise route',exact:true}).click();
      await expect(page.locator('#studio-floor .floor-station.production')).toHaveCount(12);
      await noOverflow(page);
      await page.screenshot({path:'tmp/screenshots/route-studio-mobile.png',fullPage:true});
      await page.getByRole('button',{name:'Close dialog'}).click();
    }
  }
  await page.getByRole('button',{name:'Open navigation'}).click();
  await page.getByRole('button',{name:'Overview',exact:true}).click();
  await page.screenshot({path:'tmp/screenshots/manager-mobile.png',fullPage:true});
  expect(errors).toEqual([]);
});

test('staff registration separates machine, quality, and warehouse jobs',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await page.getByRole('button',{name:'Register as staff'}).click();
  await expect(page.getByLabel('Production machine',{exact:true})).toBeVisible();
  await expect(page.locator('[name="machine_id"] option')).toHaveCount(13);
  await expect(page.locator('[name="machine_id"]')).not.toContainText('Quality inspection');
  await expect(page.locator('[name="machine_id"]')).not.toContainText('Warehouse');
  await page.getByLabel('Job role').selectOption('qc');
  await expect(page.locator('[name="machine_id"]')).toContainText('QC');
  await expect(page.locator('[name="machine_id"] option')).toHaveCount(2);
  await page.getByLabel('Job role').selectOption('storage');
  await expect(page.locator('[name="machine_id"]')).toContainText('Warehouse');
  await expect(page.locator('[name="machine_id"] option')).toHaveCount(2);
  await page.screenshot({path:'tmp/screenshots/staff-registration.png',fullPage:true});
  await page.locator('[name="machine_id"]').selectOption('5');
  await page.getByLabel('Full name',{exact:true}).fill('Warehouse registration check');
  await page.getByLabel('Email address',{exact:true}).fill('warehouse-ui@example.com');
  await page.getByLabel('Password',{exact:true}).fill('RegisterPassword123!');
  await page.getByRole('button',{name:'Request manager approval'}).click();
  await expect(page.getByRole('heading',{name:'Request submitted'})).toBeVisible();
  await noOverflow(page);
});

test('operator phone decodes a real QR image and records work',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await login(page,'operator');
  await noOverflow(page);
  await page.screenshot({path:'tmp/screenshots/operator-mobile.png',fullPage:true});
  const state=await (await page.request.get('/api/state')).json();
  const first=state.operations.filter(o=>o.seq===1).sort((a,b)=>a.forecast_start.localeCompare(b.forecast_start))[0];
  const qr=await page.request.get(`/api/lots/${first.lot_id}/qr`);
  expect(qr.ok()).toBeTruthy();
  await page.getByRole('button',{name:'Scan a lot',exact:true}).last().click();
  await page.locator('#qr-file').setInputFiles({name:'lot.svg',mimeType:'image/svg+xml',buffer:await qr.body()});
  await expect(page.locator('#scan-result')).toContainText(first.lot_code);
  await expect(page.getByRole('button',{name:'Start operation',exact:true})).toBeVisible();
  await noOverflow(page);
  await page.getByRole('button',{name:'Start operation',exact:true}).click();
  await expect(page.getByRole('button',{name:'Complete operation',exact:true})).toBeVisible();
  await page.screenshot({path:'tmp/screenshots/scanner-mobile.png',fullPage:true});
  await page.getByRole('button',{name:'Complete operation',exact:true}).click();
  await expect(page.getByText('Step 2 of 5',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Close dialog'}).click();
  await page.getByRole('button',{name:'Open navigation'}).click();
  await page.getByRole('button',{name:'My work queue',exact:true}).click();
  await noOverflow(page);
});

test('client sees a private responsive order portal',async({page})=>{
  await page.setViewportSize({width:360,height:800});
  await login(page,'client');
  await expect(page.getByRole('heading',{name:'Your production, at a glance.'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Machine schedule',exact:true})).toHaveCount(0);
  await noOverflow(page);
  await page.screenshot({path:'tmp/screenshots/client-mobile.png',fullPage:true});
  await page.setViewportSize({width:1366,height:900});
  await noOverflow(page);
  await page.screenshot({path:'tmp/screenshots/client-desktop.png',fullPage:true});
});
