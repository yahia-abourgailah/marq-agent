# marq-agent

CRM agent for Marq CRM.

## Branching strategy

| Branch    | Environment | Purpose                                              |
|-----------|-------------|------------------------------------------------------|
| `dev`     | development | Default working branch. All feature branches cut from and merged back here. |
| `staging` | staging     | Pre-production validation. Receives merges from `dev`. |
| `main`    | production  | Released code only. Receives merges from `staging`.   |

Flow: `feature/*` → `dev` → `staging` → `main`

Hotfixes: `hotfix/*` cut from `main`, merged to `main`, then back-merged into `staging` and `dev`.

## Environments

Each branch maps to one environment, configured entirely through environment variables.

| Environment | Template                   | Local file          |
|-------------|----------------------------|---------------------|
| development | `.env.development.example` | `.env.development`  |
| staging     | `.env.staging.example`     | `.env.staging`      |
| production  | `.env.production.example`  | `.env.production`   |

Real `.env.*` files are gitignored — never commit secrets. Copy the template for the
environment you are running against:

```bash
cp .env.development.example .env.development
```

`APP_ENV` selects the active environment at runtime.
