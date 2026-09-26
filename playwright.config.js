const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 45000,
  workers: 1,
  use: { baseURL:'http://127.0.0.1:5001', channel:'msedge', headless:true, screenshot:'only-on-failure', trace:'retain-on-failure' },
  webServer: { command:'python tests/serve_ui.py', url:'http://127.0.0.1:5001', reuseExistingServer:false, timeout:30000 },
});
