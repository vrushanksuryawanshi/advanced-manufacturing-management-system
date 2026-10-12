# Vercel deployment

The application is configured to run as one Flask Vercel Function and to use a
hosted PostgreSQL database. SQLite remains the default for local development and
tests, but must not be used in production on Vercel.

## One-time setup

1. Commit and push the deployment files to the GitHub `main` branch.
2. Sign in to Vercel with GitHub and choose **Add New > Project**.
3. Import `vrushanksuryawanshi/advanced-manufacturing-management-system`.
4. Keep the project root as `.` and leave Framework Preset, Build Command, and
   Output Directory on their automatic/default settings. Complete the import.
   The first deployment may report that `DATABASE_URL` is missing; this is an
   intentional safety check, and the deployment must not be used with SQLite.
5. Open the project's **Storage** or **Marketplace** section, add **Neon
   Postgres**, create a database in a nearby region, and connect it to this
   project. Verify that the integration created `DATABASE_URL` under **Settings
   > Environment Variables**. In **Settings > Functions**, place the function in
   the same or nearest available region as the database.
6. Add the following Production environment variables:

   - `SECRET_KEY`: a random value of at least 64 characters.
   - `COOKIE_SECURE`: `1`.
   - `FACTORY_UTC_OFFSET_MINUTES`: `330` for India, or the factory's UTC offset.
   - `INITIAL_MANAGER_NAME`: the first manager's full name.
   - `INITIAL_MANAGER_EMAIL`: the first manager's real email address.
   - `INITIAL_MANAGER_PASSWORD`: a unique password with at least 12 characters.
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_FROM`:
     credentials from an SMTP email provider. These are required before the
     manager can create and email credentials to client accounts.

   Generate a suitable secret locally in PowerShell with:

   ```powershell
   python -c "import secrets; print(secrets.token_hex(48))"
   ```

7. Deploy the `main` branch. On its first start, the application creates the
   PostgreSQL tables and the initial manager only if the database has no users.
8. Open the generated `*.vercel.app` URL and sign in using the initial manager
   email and password.
9. After the first successful login, delete `INITIAL_MANAGER_NAME`,
   `INITIAL_MANAGER_EMAIL`, and `INITIAL_MANAGER_PASSWORD` from Vercel, then
   redeploy. They are bootstrap credentials and should not remain configured.

## Acceptance check

Before giving users access, verify all of the following on the deployed URL:

1. Manager login works and the page refresh keeps the session.
2. Add production, QC, and storage machines.
3. Register an operator in a private/incognito window, then approve that account.
4. Configure a product route and create a client account; confirm its email arrives.
5. Create an order and open/print its QR code.
6. Use an HTTPS phone browser to scan the QR code.
7. Complete a production step, QC pass/fail, storage, and dispatch workflow.
8. Redeploy once and confirm that users and orders still exist afterward.

## Ongoing deployment

Every commit pushed to `main` produces a new production deployment through the
GitHub integration. Other branches and pull requests receive preview deployments.
Use a separate preview database or disable preview deployments if preview data
must never share the production database.

Database backups are managed from Neon. Enable an appropriate retention policy
before using the system for real factory records.
