-- Persisted model rows captured before the Sonnet 5 pricing update.
CREATE TABLE analytics_model_pricing (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  effective_from TEXT NOT NULL,
                  effective_to TEXT,
                  input_usd_per_mtok REAL NOT NULL,
                  output_usd_per_mtok REAL NOT NULL,
                  cached_input_usd_per_mtok REAL NOT NULL DEFAULT 0,
                  notes TEXT
                );
INSERT INTO analytics_model_pricing VALUES (29, 'anthropic', 'claude-sonnet-4-6', '2020-01-01T00:00:00Z', NULL, 3.0, 15.0, 0.3, 'seed');
INSERT INTO analytics_model_pricing VALUES (28, 'anthropic', 'claude-sonnet-5', '2020-01-01T00:00:00Z', NULL, 3.0, 15.0, 0.3, 'seed');
