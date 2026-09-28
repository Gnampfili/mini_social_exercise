SELECT COUNT(*) AS lurkers
FROM users u
WHERE NOT EXISTS (
    SELECT 1 FROM posts p
    WHERE p.user_id = u.id
)
AND NOT EXISTS (
    SELECT 1 FROM comments c
    WHERE c.user_id = u.id
)
AND NOT EXISTS (
    SELECT 1 FROM reactions r
    WHERE r.user_id = u.id
);









WITH engagement AS (
    SELECT
        p.user_id,
        COUNT(DISTINCT c.id) AS comments_received,
        COUNT(DISTINCT r.id) AS reactions_received
    FROM posts p
    LEFT JOIN comments c ON c.post_id = p.id
    LEFT JOIN reactions r ON r.post_id = p.id
    GROUP BY p.user_id
)
SELECT
    u.username,
    e.comments_received,
    e.reactions_received,
    e.comments_received + e.reactions_received AS total_engagement
FROM engagement e
JOIN users u ON u.id = e.user_id
ORDER BY total_engagement DESC
LIMIT 5;



WITH engagement_times AS (
    SELECT
        p.id AS post_id,
        p.created_at AS published_at,
        MIN(c.created_at) AS first_engagement_at,
        MAX(c.created_at) AS last_engagement_at
    FROM posts p
    JOIN comments c ON c.post_id = p.id
    GROUP BY p.id
)
SELECT
    COUNT(*) AS posts_with_comments,
    AVG(
        (julianday(first_engagement_at) - julianday(published_at))
        * 86400
    ) AS average_seconds_to_first_engagement,
    AVG(
        (julianday(last_engagement_at) - julianday(published_at))
        * 86400
    ) AS average_seconds_to_last_engagement
FROM engagement_times;