-- Squadfire: multi-region database (D7, D10).
-- Nodes are started with --locality=region=us-east | eu-west | af-west.
CREATE DATABASE IF NOT EXISTS squadfire;
ALTER DATABASE squadfire SET PRIMARY REGION 'us-east';
ALTER DATABASE squadfire ADD REGION IF NOT EXISTS 'eu-west';
ALTER DATABASE squadfire ADD REGION IF NOT EXISTS 'af-west';

-- One node per region, so keep backup copies of every fragment in the other
-- regions: the database survives the loss of a whole region (D10).
ALTER DATABASE squadfire SURVIVE REGION FAILURE;
