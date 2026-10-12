# Brief - switch LightRAG ingestion LLM to sigma qwen3.8-flash + canary (NO wipe)

Operator decision 2026-10-12. Backup to deltahome is done and verified (it contains the old .env, so
no further backup is needed; the old values are listed below for rollback). Do NOT delete or wipe
anything in this task. Keep rag-ingest-worker STOPPED throughout (leave it stopped at the end).

## Change (only these three keys in ~/rag-project/.env; do not print the file or any other key)
- LLM_MODEL=qwen3.8-flash            (old: qwen3.6-35b)
- LLM_BINDING_HOST=http://192.168.1.195:1234/v1   (old: http://192.168.1.190:1234/v1)
- LLM_BINDING_API_KEY=1234           (old: a different key; sigma needs "Authorization: Bearer 1234")
Leave all EMBEDDING_* vars untouched (embeddings stay on 192.168.1.190). Use sed -i on exactly those
lines; show me only a `grep -E '^(LLM_MODEL|LLM_BINDING_HOST)=' .env` afterwards (never the key lines).
Sigma already answers: `curl -s -H "Authorization: Bearer 1234" http://192.168.1.195:1234/v1/models`.

## Canary
1. `docker start lightrag-server` (worker stays stopped). Wait until `/health` answers.
2. Confirm the container sees the new model: `docker exec lightrag-server env | grep -E '^(LLM_MODEL|LLM_BINDING_HOST)='`.
3. POST a ~1500-character synthetic text to `/documents/text` (use LIGHTRAG_API_KEY from .env via shell
   variable, never echo it). Content: invented company "Acme Rail" with 4-5 named people, 3 systems and
   relations between them, file_source `canary-qwen38-flash.txt`. Record the time.
4. Poll `/documents/track_status/<track_id>` every 15 s up to 10 min. Record status, total time,
   and the doc's chunk count.
5. Check extraction worked: entity/relation count for the doc (graph endpoints or the graphml/kv files in
   lightrag_store; counts only). Check `docker logs lightrag-server --tail 60` for errors/timeouts/JSON
   parse failures from the LLM (report error lines, not secrets).
6. One query: POST /query {"query":"Who works at Acme Rail and which systems do they use?","mode":"mix"}
   and report whether the answer matches the canary text.

## Acceptance / report
Short heredoc report (not write_file) to ~/rag-project/docs/ops/runs/model-switch-canary.md:
env lines (non-secret), canary status + seconds, chunks, entities, relations, any log errors, query answer
(first 300 chars). If the canary fails: restore the three old values (above; old key: take it from the
backup `config.tgz` on deltahome, extract to /tmp only, delete the /tmp copy after) and report - do not retry
with other settings. Constraints: no rm/mv/chown/chmod beyond removing your own /tmp extract; if an approval dialog shows a
command not listed here, do not accept it.
