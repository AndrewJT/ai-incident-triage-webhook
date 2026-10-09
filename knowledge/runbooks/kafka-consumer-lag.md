# Kafka consumer lag

## Symptoms
"consumer lag exceeded threshold", growing lag on a consumer group, delayed downstream processing,
frequent consumer group rebalances, or `CommitFailedException`.

## Diagnosis
- Check lag per partition: one hot partition versus uniform lag.
- Check consumer processing time per message and recent deploys.
- Look for rebalance loops caused by `max.poll.interval.ms` being exceeded.

## Remediation
1. Scale out consumers up to the number of partitions.
2. Fix or skip a poison message blocking a partition (send to a dead-letter topic).
3. Increase `max.poll.interval.ms` or reduce `max.poll.records` if rebalancing repeatedly.
4. Roll back the consumer deploy if processing time regressed.
