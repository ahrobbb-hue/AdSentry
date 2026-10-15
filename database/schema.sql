-- ==========================================
-- AdSentry SQLite Database Schema
-- ==========================================

-- 1. Submissions Table: Stores ad applications from submission to review
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    user_avatar_url TEXT,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', -- 'PENDING', 'APPROVED', 'REJECTED'
    review_message_id INTEGER,              -- Discord message ID in staff review channel
    reviewed_by INTEGER,                   -- Moderator User ID
    rejection_reason TEXT,                 -- Reason sent to user upon rejection
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMP
);

-- 2. Active Posts Table: Tracks published announcements for Auto-Expiry (Self-Cleaning)
CREATE TABLE IF NOT EXISTS active_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,           -- Public announcement channel ID
    message_id INTEGER NOT NULL,           -- Published message ID (posted via webhook)
    webhook_url TEXT,                      -- Webhook used for publishing
    published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,                  -- Expiration timestamp (NULL indicates 'Forever')
    is_expired INTEGER NOT NULL DEFAULT 0, -- 0 = Active, 1 = Expired and deleted
    FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
);

-- 3. User Cooldowns Table: Tracks anti-spam and submission rate limits
CREATE TABLE IF NOT EXISTS user_cooldowns (
    user_id INTEGER PRIMARY KEY,
    last_submission_at TIMESTAMP NOT NULL,
    cooldown_until TIMESTAMP NOT NULL
);

-- Indexes for optimal querying performance
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id);
CREATE INDEX IF NOT EXISTS idx_active_posts_expiry ON active_posts(expires_at, is_expired);
CREATE INDEX IF NOT EXISTS idx_user_cooldowns_until ON user_cooldowns(cooldown_until);
