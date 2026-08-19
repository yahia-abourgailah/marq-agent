# Testing the API by hand

For working on the API before the website front end exists. Postman is the
main tool; `curl` covers the one thing Postman is bad at.

---

## One-time setup

```bash
python scripts/dev_token.py init
```

Generates a development keypair under `var/dev-jwt/` (gitignored, `0600` on
the private half) and prints the four lines to add to `.env.development`.
They are already there if you are on this branch.

**Why a keypair rather than `AUTH_DEV_MODE=true`.** Dev mode accepts an
unsigned `X-Debug-Subject` header — it is a *bypass*, and it never runs the
signature, expiry, issuer or audience checks. Testing only that means the
first real token from the website is the first time the verification code has
ever executed. With a local key you exercise the path that ships.

Dev mode is still there for a five-second smoke test:

```bash
AUTH_DEV_MODE=true python main.py
curl localhost:8000/v1/chat -H 'X-Debug-Subject: you@example.com' ...
```

It refuses to start under `APP_ENV=production`.

---

## Running it

```bash
python main.py                     # 127.0.0.1:8000
```

Interactive docs at http://127.0.0.1:8000/docs — FastAPI renders them from the
same schema the routes use, so they cannot go stale.

Check it is actually working, not merely listening:

```bash
curl -s localhost:8000/health/ready | python -m json.tool
```

`vector_index: false` is expected without a Qdrant server. It does not block
readiness on purpose: search degrades while reconciliation and the CRM agent
keep working. `checkpointer: false` would be serious — it means conversations
would not survive a restart.

---

## Postman

Import both files from `docs/postman/`:

- `marq-agent.postman_collection.json` — 19 requests in 5 folders
- `marq-agent.postman_environment.json` — `base_url` and `token`

Then:

1. Select the **MarQ Agent — local** environment (top right).
2. `python scripts/dev_token.py mint` and paste the output into `token`.
3. Send **Chat -> Ask a question**.

Auth is set once at the collection level, so every request inherits it.
`thread_id` and `file_id` are captured by test scripts, which is why the
Threads folder works without copy-pasting ids — run a chat request first.

### What is in there

| Folder | |
|---|---|
| Health | Liveness and readiness, no token needed |
| Chat | A question, a follow-up in the same thread, streaming, out-of-scope |
| Threads | List, replay, delete |
| Workspace | Upload, list, ask about the file, delete |
| Negative tests | Six requests that **should** fail — the boundaries |

The negative tests are the interesting folder. They assert that no token is a
401, that a body containing `workspace_id` or `requester_id` is a **422 rather
than a silently ignored field**, and that another employee's thread is a 404
rather than a 403 — a 403 would confirm which thread ids exist.

### Two things worth knowing

**Uploads.** Postman cannot store a file in an exported collection, so open
the *Upload a file* request's Body tab and pick one yourself. Any `.xlsx`,
`.csv`, `.tsv` or `.pdf` up to 25 MB.

**Streaming.** Postman buffers `text/event-stream` and shows one blob at the
end rather than live frames. The request is included so you can inspect the
response, but to actually watch a stream:

```bash
curl -N -X POST localhost:8000/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $(python scripts/dev_token.py mint)" \
  -d '{"message":"Which franchise has the most cancellations?"}'
```

Frames arrive as `start`, `route`, `tool`, many `token`, then `final`. A
client can ignore `token` entirely and read `final`, which carries the whole
answer.

---

## Running the collection headlessly

The collection is also a test suite. With [newman](https://github.com/postmanlabs/newman):

```bash
npm install -g newman

newman run docs/postman/marq-agent.postman_collection.json \
  -e docs/postman/marq-agent.postman_environment.json \
  --env-var "token=$(python scripts/dev_token.py mint)" \
  --timeout-request 120000
```

Skip the Workspace folder there — it needs a file picked interactively:

```bash
  --folder Health --folder Chat --folder Threads --folder "Negative tests"
```

---

## Known-good answers

The agent reads the local `marq_agent_dev` fixture — 40 users, 1000 leads, 350
deals — so the same question gives the same number every time. Useful when you
want to know whether a change broke an answer:

| Question | Answer | Check it |
|---|---|---|
| How many deals are there in total? | 315 | `SELECT count(*) FROM deals WHERE deleted_at IS NULL` |
| How many leads are stale? | varies with the date | `SELECT count(*) FROM leads WHERE is_stale` |

**Verify against SQL, not against "it didn't error".** Nearly every bug found
in this project was a confident wrong number rather than an exception.

You no longer need to guess which query to check against — every chat
response carries `provenance` with the SQL that produced the answer:

```bash
curl -s -X POST localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $(python scripts/dev_token.py mint)" \
  -d '{"message":"How many deals are contracted versus cancelled?"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['provenance'][0]['sql'])"
```

Paste the result into psql. If it does not reproduce the answer, that is a
real bug and worth reporting.

---

## When something fails

**401 on everything.** The token expired (24h default), or the issuer and
audience in `.env.development` do not match what signed it. The API reports
every auth failure identically on purpose, so it will not tell you which —
this will:

```bash
python scripts/dev_token.py check
```

**The server will not start**, with a message about `JWT_PUBLIC_KEY`. There is
no key configured and dev mode is off. Run `dev_token.py init`, or start with
`AUTH_DEV_MODE=true`. Failing at boot is deliberate: a service that starts
without key material and 500s on the first real question has turned a
configuration error into an intermittent one.

**A 500 with a `request_id`.** The response deliberately says nothing else —
internals do not cross the wire. Find the detail in the server log by that id.

**The frontend gets CORS errors.** `CORS_ORIGINS` is empty, which blocks
browsers. Postman is not a browser and ignores CORS entirely, so this only
appears once a real front end connects. Set it then:

```
CORS_ORIGINS=http://localhost:3000
```

---

## Handing the contract to the frontend team

`docs/openapi.json` is the exported schema — they can generate a typed client
from it rather than reading prose. Regenerate after changing any route:

```bash
python -c "import json; from main import app; print(json.dumps(app.openapi(), indent=2))" > docs/openapi.json
```

The parts worth saying out loud, because a generated client will not:

- **Identity is never a request field.** `requester_id` and `workspace_id`
  come from the token. Sending either is a 422.
- **`thread_id` is theirs to choose.** It is scoped to the authenticated
  caller, so two users may both use `"today"` and will never collide. Omit it
  to start a new conversation; the response carries the one that was created.
- **Errors are always the same shape**: `{"error": {"code", "message",
  "request_id"}}`. `message` is safe to show a user; `code` is what to branch
  on.
- **A turn takes seconds**, not milliseconds — two to three model calls. Use
  `/v1/chat/stream` for anything user-facing.
