-- Persisted model rows captured before the Sonnet 5 pricing update.
CREATE TABLE models (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'anthropic',
                model_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                tier TEXT NOT NULL DEFAULT '',
                context_window INTEGER NOT NULL DEFAULT 200000,
                is_1m INTEGER NOT NULL DEFAULT 0,
                input_price REAL NOT NULL DEFAULT 0,
                output_price REAL NOT NULL DEFAULT 0,
                cached_input_price REAL NOT NULL DEFAULT 0,
                cache_write_5m_price REAL,
                cache_write_1h_price REAL,
                supports_thinking INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                UNIQUE(provider, model_id)
            );
INSERT INTO models VALUES ('anthropic/claude-sonnet-4-6', 'anthropic', 'claude-sonnet-4-6', 'Claude Sonnet 4.6', 'Fast + smart. Daily driver.', 'sonnet', 1000000, 1, 3.0, 15.0, 0.3, 3.75, 6.0, 1, 1, 20, 1791047895.5745668, 1791047895.5745668);
INSERT INTO models VALUES ('anthropic/claude-sonnet-5', 'anthropic', 'claude-sonnet-5', 'Claude Sonnet 5', 'Current Sonnet (2026-06). Best speed+intelligence balance — daily driver. 1M context; adaptive thinking, effort defaults to high. Intro pricing $2/$10 through Aug 2026.', 'sonnet', 1000000, 1, 3.0, 15.0, 0.3, 3.75, 6.0, 1, 1, 15, 1791047895.5745668, 1791047895.5745668);
