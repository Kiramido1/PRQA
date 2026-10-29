# n8n workflows (generated)

These files are produced by `scripts/build/build_workflows.py` — edit the generator, not the JSON.

```bash
python3 scripts/build/build_workflows.py      # regenerate
python3 scripts/build/validate_workflows.py   # static checks
scripts/setup/import_workflows.sh             # import into the running n8n (keeps active=false)
```

| File | Workflow id | Trigger |
|---|---|---|
| `00-prqa-main-pipeline.json` | `PrqaMainPipeline` | Execute Workflow Trigger + Manual Synthetic Test Trigger |
| `01-prqa-github-webhook-receiver.json` | `PrqaWebhookRecvr` | Webhooks `POST /webhook/prqa/github` (GitHub) and `POST /webhook/prqa/actions` (GitHub Actions) — production entry points |
| `02-prqa-pr-context-collector.json` | `PrqaPrContextCol` | Execute Workflow Trigger |
| `03-prqa-llm-prompt-builder.json` | `PrqaLlmPromptBld` | Execute Workflow Trigger |
| `04-prqa-opencode-job-dispatcher.json` | `PrqaOpencodeDisp` | Execute Workflow Trigger |
| `05-prqa-result-validation-report.json` | `PrqaResultValidn` | Execute Workflow Trigger |
| `06-prqa-discord-reporter.json` | `PrqaDiscordRepor` | Execute Workflow Trigger |
| `07-prqa-github-pr-commenter.json` | `PrqaGithubCommen` | Execute Workflow Trigger |
| `08-prqa-error-handler.json` | `PrqaErrorHandler` | Error Trigger |

Runtime values come from `$env` (set in `compose/docker-compose.yml` from `compose/.env`): `PRQA_WORKER_URL`, `PRQA_WORKER_TOKEN`, `PRQA_AUTOMATION_MODE`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN`, `GROQ_API_KEY` (prompt LLM, active provider; from `~/.config/prqa/secrets.env`), `PROMPT_LLM_API_KEY` (legacy non-Groq endpoints only), `DISCORD_WEBHOOK_URL` (error-handler alerts only). `PRQA_WEBHOOK_SECRET` (GitHub Actions entry) is read by the **worker** only. The exports contain no secrets.

The GitHub Actions workflow for target repositories is generated separately: `python3 scripts/build/build_actions_workflow.py` → `.github/workflows/prqa.yml` (embeds `scripts/clients/prqa_actions_client.py`).
