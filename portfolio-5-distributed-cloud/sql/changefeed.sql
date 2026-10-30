-- Portfolio 5.1: stream CockroachDB changes into Kafka (run once, after make seed
-- and make up-stream).  make p5-changefeed
--
-- Rangefeeds are the per-range "tell me about changes" mechanism changefeeds are
-- built on; they are off by default. Since v24.3 changefeeds need no licence key
-- (an unlicensed cluster is only throttled after a 7-day grace period).
SET CLUSTER SETTING kv.rangefeed.enabled = true;
SET DATABASE = squadfire;

-- One topic per table (squadfire.<table>). initial_scan sends the existing
-- history first; resolved markers say "everything up to this time is sent";
-- updated adds each row's commit timestamp. Delivery is at-least-once, so the
-- lake de-duplicates on (key, updated).
CREATE CHANGEFEED FOR TABLE purchase, purchase_line, inventory, item, region
  INTO 'kafka://kafka:9092?topic_prefix=squadfire.'
  WITH initial_scan = 'yes', format = json, resolved = '10s', updated;
