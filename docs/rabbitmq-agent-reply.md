# RabbitMQ Agent Reply Pipeline

The production Docker Compose path uses RabbitMQ for `agent.reply.requested`.
PostgreSQL remains the source of truth, while Redis Streams remains the SSE
transport to the browser.

```text
POST /conversations/{id}/messages
  -> one PostgreSQL transaction
     -> messages
     -> agent_runs (queued)
     -> outbox_events (PENDING)
  -> 202 Accepted
  -> outbox-relay
  -> customer_service.commands / agent.reply.requested
  -> agent.reply.q
  -> agent-worker
  -> Redis Streams assistant.started/delta/completed
  -> messages + agent_runs (completed)
  -> RabbitMQ ACK
```

Only identifiers are carried in RabbitMQ. Prompts, conversation history,
credentials, policy text, and evidence bytes stay in PostgreSQL, Redis, and
MinIO.

## Local operation

Docker Compose sets `AGENT_TASK_BACKEND=rabbitmq` for the backend. The default
application setting is `inline`, which keeps unit tests and standalone local
development independent of RabbitMQ.

```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f outbox-relay agent-worker
```

RabbitMQ management UI: <http://localhost:15672>

Development credentials come from `RABBITMQ_USER` and `RABBITMQ_PASSWORD`.
Change them before any shared or production deployment.

## Queues

- `agent.reply.q`: main command queue.
- `agent.reply.q.retry.1`: 5-second retry delay.
- `agent.reply.q.retry.2`: 30-second retry delay.
- `agent.reply.q.retry.3`: 120-second retry delay.
- `customer_service.dlq`: commands that exhausted retries or were invalid.

Publisher confirms and mandatory routing are enabled. Consumers ACK only after
the run reaches a durable result or after a retry/DLQ command is confirmed.
Duplicate delivery is handled through the durable `AgentRun` status. A Redis
lease serializes work for the same conversation.

## Useful checks

```powershell
docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready messages_unacknowledged
docker compose exec postgres psql -U customer_service -d customer_service -c "select event_type,status,count(*) from outbox_events group by 1,2 order by 1,2;"
docker compose exec postgres psql -U customer_service -d customer_service -c "select status,count(*) from agent_runs group by 1 order by 1;"
```

If RabbitMQ is unavailable, accepted requests remain in `outbox_events` as
`PENDING`. The relay publishes them after connectivity returns. Do not start a
second database-polling consumer for `agent.reply.requested`.
