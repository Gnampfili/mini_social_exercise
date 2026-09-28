from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import math
import sqlite3
import hashlib
import re
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

# ----- Groups feature -----
# Roles are ordered from least to most privileged.
GROUP_ROLES = ['member', 'moderator', 'admin']


def init_group_tables():
    """
    One-time startup migration for the Groups feature. Creates the 'groups'
    and 'group_members' tables if they don't exist yet, and adds a nullable
    'group_id' column to 'posts' so a post can optionally belong to a group.
    Safe to run every time the app starts (all operations are idempotent).
    """
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            created_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            UNIQUE(group_id, user_id)
        )
    ''')

    # Add posts.group_id if it's missing (older databases won't have it yet).
    cur.execute("PRAGMA table_info(posts)")
    post_columns = [row[1] for row in cur.fetchall()]
    if 'group_id' not in post_columns:
        cur.execute('ALTER TABLE posts ADD COLUMN group_id INTEGER DEFAULT NULL')

    conn.commit()
    conn.close()


init_group_tables()


def get_group_role(user_id, group_id):
    """Returns the caller's role ('admin'/'moderator'/'member') in a group, or None."""
    if not user_id or not group_id:
        return None
    row = query_db('SELECT role FROM group_members WHERE group_id = ? AND user_id = ?',
                    (group_id, user_id), one=True)
    return row['role'] if row else None


def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   p.group_id, gr.name as group_name,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN groups gr ON p.group_id = gr.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   p.group_id, gr.name as group_name
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN groups gr ON p.group_id = gr.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    #  3.5 Groups the current user can post into (for the "create post" form) 
    user_groups = []
    if current_user_id:
        user_groups = query_db('''
            SELECT g.id, g.name
            FROM groups g
            JOIN group_members gm ON gm.group_id = g.id
            WHERE gm.user_id = ?
            ORDER BY g.name ASC
        ''', (current_user_id,))

    #  4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           user_groups=user_groups)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Optional: post into one of the groups the user belongs to
    group_id = request.form.get('group_id') or None
    if group_id:
        is_member = query_db('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?',
                             (group_id, user_id), one=True)
        if not is_member:
            flash('You can only post to groups you are a member of.', 'danger')
            return redirect(url_for('feed'))

    # Pass the user's content through the moderation function
    moderated_content = content

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content, group_id) VALUES (?, ?, ?)',
                   (user_id, moderated_content, group_id))
        db.commit()
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  Groups this user belongs to (click through to see/join from there) 
    user_groups = query_db('''
        SELECT g.id, g.name, gm.role
        FROM group_members gm
        JOIN groups g ON gm.group_id = g.id
        WHERE gm.user_id = ?
        ORDER BY g.name ASC
    ''', (user['id'],))

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following,
                           user_groups=user_groups)
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
               p.group_id, gr.name as group_name
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN groups gr ON p.group_id = gr.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        flash('Your comment was added.', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        # Rule 2.1, Step 1: calculate the post's base Content Score.
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        # Rule 2.1, Step 2: determine the author's account age in days.
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        # Rule 2.1, Step 3: apply the new-account multiplier when applicable.
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        # Rule 2.1, Step 1: calculate the comment's base Content Score.
        _, score = moderate_content(comment_dict['content'])
        # Rule 2.1, Step 2: determine the author's account age in days.
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        # Rule 2.1, Step 3: apply the new-account multiplier when applicable.
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

#  Groups 

@app.route('/groups')
def groups_list():
    """Browse all groups. Logged-in users can also create a new one from here."""
    current_user_id = session.get('user_id')

    groups_raw = query_db('''
        SELECT g.id, g.name, g.description, g.created_at, u.username as creator_username,
               (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) as member_count
        FROM groups g
        JOIN users u ON g.created_by = u.id
        ORDER BY g.created_at DESC
    ''')

    groups = []
    for grp in groups_raw:
        grp_dict = dict(grp)
        grp_dict['my_role'] = get_group_role(current_user_id, grp_dict['id'])
        groups.append(grp_dict)

    return render_template('groups.html.j2', groups=groups)


@app.route('/groups/new', methods=['POST'])
def create_group():
    """Creates a new group. The creator automatically becomes its admin."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to create a group.', 'danger')
        return redirect(url_for('login'))

    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()

    if not name:
        flash('A group needs a name.', 'warning')
        return redirect(url_for('groups_list'))

    db = get_db()
    try:
        cur = db.execute('INSERT INTO groups (name, description, created_by) VALUES (?, ?, ?)',
                          (name, description, user_id))
        db.commit()
        new_group_id = cur.lastrowid

        # The creator is automatically the group's first admin.
        db.execute('INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)',
                   (new_group_id, user_id, 'admin'))
        db.commit()

        flash(f'Group "{name}" was created!', 'success')
        return redirect(url_for('group_detail', group_id=new_group_id))
    except sqlite3.IntegrityError:
        flash('A group with that name already exists.', 'danger')
        return redirect(url_for('groups_list'))


@app.route('/groups/<int:group_id>')
def group_detail(group_id):
    """Shows a group's info, its members grouped by role, and recent posts in it."""
    group_raw = query_db('''
        SELECT g.id, g.name, g.description, g.created_at, u.username as creator_username
        FROM groups g
        JOIN users u ON g.created_by = u.id
        WHERE g.id = ?
    ''', (group_id,), one=True)

    if not group_raw:
        abort(404)

    group = dict(group_raw)
    current_user_id = session.get('user_id')
    my_role = get_group_role(current_user_id, group_id)

    members_raw = query_db('''
        SELECT u.id as user_id, u.username, gm.role, gm.joined_at
        FROM group_members gm
        JOIN users u ON gm.user_id = u.id
        WHERE gm.group_id = ?
        ORDER BY CASE gm.role WHEN 'admin' THEN 0 WHEN 'moderator' THEN 1 ELSE 2 END, gm.joined_at ASC
    ''', (group_id,))
    members = [dict(m) for m in members_raw]

    posts_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.group_id = ?
        ORDER BY p.created_at DESC
        LIMIT 20
    ''', (group_id,))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        posts.append(post_dict)

    return render_template('group_detail.html.j2',
                           group=group,
                           members=members,
                           my_role=my_role,
                           posts=posts,
                           group_roles=GROUP_ROLES)


@app.route('/groups/<int:group_id>/join', methods=['POST'])
def join_group(group_id):
    """Lets the current user join a group as a regular member."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to join a group.', 'danger')
        return redirect(url_for('login'))

    group = query_db('SELECT id, name FROM groups WHERE id = ?', (group_id,), one=True)
    if not group:
        flash('Group not found.', 'danger')
        return redirect(url_for('groups_list'))

    db = get_db()
    try:
        db.execute('INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)',
                   (group_id, user_id, 'member'))
        db.commit()
        flash(f'You joined "{group["name"]}".', 'success')
    except sqlite3.IntegrityError:
        flash('You are already a member of this group.', 'info')

    return redirect(url_for('group_detail', group_id=group_id))


@app.route('/groups/<int:group_id>/leave', methods=['POST'])
def leave_group(group_id):
    """Lets the current user leave a group (unless they're its last admin)."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to leave a group.', 'danger')
        return redirect(url_for('login'))

    role = get_group_role(user_id, group_id)
    if not role:
        flash('You are not a member of this group.', 'info')
        return redirect(url_for('group_detail', group_id=group_id))

    if role == 'admin':
        remaining_admins = query_db(
            "SELECT COUNT(*) as cnt FROM group_members WHERE group_id = ? AND role = 'admin' AND user_id != ?",
            (group_id, user_id), one=True)['cnt']
        if remaining_admins == 0:
            flash('You are the only admin left. Promote someone else to admin before leaving.', 'warning')
            return redirect(url_for('group_detail', group_id=group_id))

    db = get_db()
    db.execute('DELETE FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, user_id))
    db.commit()
    flash('You left the group.', 'info')
    return redirect(url_for('group_detail', group_id=group_id))


@app.route('/groups/<int:group_id>/members/<int:member_user_id>/role', methods=['POST'])
def change_group_role(group_id, member_user_id):
    """Admin-only: change a member's role between member, moderator and admin."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to do that.', 'danger')
        return redirect(url_for('login'))

    acting_role = get_group_role(user_id, group_id)
    if acting_role != 'admin':
        flash('Only group admins can change member roles.', 'danger')
        return redirect(url_for('group_detail', group_id=group_id))

    new_role = request.form.get('role')
    if new_role not in GROUP_ROLES:
        flash('Invalid role.', 'warning')
        return redirect(url_for('group_detail', group_id=group_id))

    target_role = get_group_role(member_user_id, group_id)
    if not target_role:
        flash('That user is not a member of this group.', 'danger')
        return redirect(url_for('group_detail', group_id=group_id))

    # Don't let the only admin demote themselves without promoting someone else first.
    if member_user_id == user_id and target_role == 'admin' and new_role != 'admin':
        remaining_admins = query_db(
            "SELECT COUNT(*) as cnt FROM group_members WHERE group_id = ? AND role = 'admin' AND user_id != ?",
            (group_id, user_id), one=True)['cnt']
        if remaining_admins == 0:
            flash('You are the only admin left. Promote someone else first.', 'warning')
            return redirect(url_for('group_detail', group_id=group_id))

    db = get_db()
    db.execute('UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?',
               (new_role, group_id, member_user_id))
    db.commit()
    flash('Member role updated.', 'success')
    return redirect(url_for('group_detail', group_id=group_id))


@app.route('/groups/<int:group_id>/members/<int:member_user_id>/remove', methods=['POST'])
def remove_group_member(group_id, member_user_id):
    """Admins can remove any member; moderators can only remove regular members."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to do that.', 'danger')
        return redirect(url_for('login'))

    acting_role = get_group_role(user_id, group_id)
    target_role = get_group_role(member_user_id, group_id)

    if not target_role:
        flash('That user is not a member of this group.', 'danger')
        return redirect(url_for('group_detail', group_id=group_id))

    can_remove = (
        (acting_role == 'admin' and member_user_id != user_id)
        or (acting_role == 'moderator' and target_role == 'member')
    )

    if not can_remove:
        flash('You do not have permission to remove this member.', 'danger')
        return redirect(url_for('group_detail', group_id=group_id))

    db = get_db()
    db.execute('DELETE FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, member_user_id))
    db.commit()
    flash('Member removed from the group.', 'success')
    return redirect(url_for('group_detail', group_id=group_id))


@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    user = query_db(
        'SELECT profile, created_at FROM users WHERE id = ?',
        (user_id,),
        one=True
    )
    if user is None:
        return 0.0

    # Rule 2.2, Step 1: calculate the user's profile Content Score.
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))

    _, profile_score = moderate_content(user['profile'] or '')

    # Rule 2.2, Steps 2-3: calculate post and comment Content Scores.
    post_scores = [moderate_content(row['content'])[1] for row in posts]
    comment_scores = [moderate_content(row['content'])[1] for row in comments]

    # Rule 2.2, Steps 2-3: average each content type, using zero when absent.
    average_post_score = sum(post_scores) / len(post_scores) if post_scores else 0.0
    average_comment_score = sum(comment_scores) / len(comment_scores) if comment_scores else 0.0

    # Rule 2.2, Step 4: combine profile, post, and comment scores with weights.
    content_risk_score = (
        profile_score
        + (average_post_score * 3.0)
        + average_comment_score
    )

    # Rule 2.2, Step 5: determine the account age and apply its multiplier.
    account_age_days = (datetime.utcnow() - user['created_at']).days
    if account_age_days < 7:
        content_risk_score *= 1.5
    elif account_age_days < 30:
        content_risk_score *= 1.2

    # Rule 2.2, Step 6: cap the final user risk score at 5.0.
    return min(5.0, content_risk_score)

    
# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    moderated_content = content
    score = 0.0

    if not moderated_content:
        return moderated_content, score
    #Rule 1.1.1
    # Tier 1: severe violations. Heavily penalized, and fully censored.
    for word in TIER1_WORDS:
        pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)
        matches = pattern.findall(moderated_content)
        if matches:
            score = 5.0
            moderated_content = "[content removed due to severe violation]"
            
            print("word was tier 1 sensored")
            return moderated_content, score
    #Rule 1.1.2
    # Tier 2: spam/scam phrases. 
    for phrase in TIER2_PHRASES:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        matches = pattern.findall(moderated_content)
        if matches:
            score = 5.0
            moderated_content = "[content removed due to spam/scam policy]"
            print("tier 2 phrase was sensored")
            return moderated_content, score
    #Rule 1.2.1
    #if 1.1 is passed we go to 1.2
    for word in TIER3_WORDS:
        pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)
        matches = pattern.findall(moderated_content)
        if matches:
            score += 2.0 * len(matches)
            moderated_content = pattern.sub(
                lambda m: '*' * len(m.group(0)),
                moderated_content
            )
            print("tier 3 word was sensored")

    # Rule 1.2.2
    url_pattern = re.compile(r'\b(?:https?://|www\.)[^\s<>"\']+', re.IGNORECASE)
    url_matches = url_pattern.findall(moderated_content)
    if url_matches:
        score += 2.0 * len(url_matches)
        moderated_content = url_pattern.sub('[link removed]', moderated_content)

    # Rule 1.2.3
    alphabetic_characters = [character for character in moderated_content if character.isalpha()]
    uppercase_characters = [character for character in alphabetic_characters if character.isupper()]
    if (len(alphabetic_characters) > 15 and
            len(uppercase_characters) / len(alphabetic_characters) > 0.70):
        score += 0.5
        
    
    return moderated_content, score

# Coding Assignment #3
# Assignment 3.1

# Common English words (plus common social-media filler/sentiment words) that
# carry little topical signal, so they're excluded when building/matching
# interest profiles for recommendations.
STOPWORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'and', 'or', 'but', 'to', 'of',
    'in', 'on', 'for', 'with', 'this', 'that', 'these', 'those', 'it', 'its',
    'i', 'you', 'we', 'my', 'your', 'our', 'their', 'they', 'he', 'she', 'his',
    'her', 'them', 'us', 'am', 'been', 'being', 'at', 'be', 'as', 'so', 'if',
    'me', 'im', 'just', 'not', 'have', 'has', 'had', 'do', 'did', 'does', 'from',
    'by', 'about', 'up', 'out', 'all', 'can', 'will', 'would', 'could', 'should',
    'here', 'there', 'now', 'when', 'while', 'after', 'before', 'still', 'even',
    'some', 'other', 'because', 'into', 'over', 'back', 'down', 'off', 'again',
    'never', 'always', 'every', 'one', 'two', 'first', 'next', 'new', 'got', 'get',
    'getting', 'went', 'going', 'come', 'came', 'made', 'make', 'making', 'start',
    'started', 'starting', 'end', 'little', 'small', 'big', 'lot', 'bit', 'more',
    'most', 'than', 'too', 'very', 'like', 'love', 'loved', 'really', 'super',
    'good', 'great', 'best', 'worth', 'amazing', 'awesome', 'nice', 'happy',
    'excited', 'feel', 'feeling', 'feels', 'today', 'day', 'time', 'life',
    'people', 'thing', 'things', 'know', 'think', 'want', 'need', 'finally',
    'finished',
}


def _tokenize(text):
    """Lowercases text and splits it into simple word tokens, dropping stopwords/short words."""
    if not text:
        return []
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return [w for w in words if len(w) > 2 and w not in STOPWORDS]


def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """

    # A content-based recommender: build a TF-IDF-weighted bag-of-words profile
    # of the topics the user has shown interest in (posts they reacted to or
    # commented on), then rank other posts by how much they overlap with it.
    # Words that show up in almost every post (like "finished" or "project")
    # get down-weighted by IDF, so distinctive topic words (like "diy") drive
    # the ranking instead.

    fallback_query = """
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
               p.group_id, gr.name as group_name
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN groups gr ON p.group_id = gr.id
        ORDER BY p.created_at DESC
        LIMIT 5
    """

    # Logged-out users have no engagement history to build a profile from.
    if not user_id:
        return query_db(fallback_query)

    #  1. Find the posts the user engaged with (reacted to or commented on) 
    engaged_posts = query_db('''
        SELECT DISTINCT p.id, p.content
        FROM posts p
        WHERE p.id IN (SELECT post_id FROM reactions WHERE user_id = ?)
           OR p.id IN (SELECT post_id FROM comments WHERE user_id = ?)
    ''', (user_id, user_id))

    if not engaged_posts:
        # No engagement history yet -> nothing to personalize on.
        return query_db(fallback_query)

    engaged_post_ids = {row['id'] for row in engaged_posts}

    #  2. Build the pool of candidate posts (posts we could recommend) 
    where_clauses = ["p.user_id != ?"]
    params = [user_id]

    if filter_following:
        where_clauses.append(
            "p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        )
        params.append(user_id)

    candidate_query = f"""
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
               p.group_id, gr.name as group_name
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN groups gr ON p.group_id = gr.id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY p.created_at DESC
        LIMIT 200
    """
    candidates = query_db(candidate_query, params)

    if not candidates:
        return query_db(fallback_query)

    #  3. Compute IDF (inverse document frequency) over engaged + candidate posts 
    # so common words are worth less and distinctive/topic words are worth more.
    corpus_token_sets = [set(_tokenize(row['content'])) for row in engaged_posts]
    corpus_token_sets += [set(_tokenize(row['content'])) for row in candidates]

    doc_frequency = collections.Counter()
    for token_set in corpus_token_sets:
        doc_frequency.update(token_set)

    num_docs = len(corpus_token_sets)

    def idf(word):
        return math.log((num_docs + 1) / (doc_frequency.get(word, 0) + 1)) + 1.0

    #  4. Build the user's interest profile 
    # Words that recur across several of the posts the user engaged with are a
    # much stronger interest signal than a word that only appears once, so the
    # profile is weighted by (how many liked posts contain the word) * idf,
    # rather than raw term frequency.
    engaged_doc_frequency = collections.Counter()
    for row in engaged_posts:
        engaged_doc_frequency.update(set(_tokenize(row['content'])))

    interest_profile = collections.Counter()
    for word, freq_in_engaged in engaged_doc_frequency.items():
        interest_profile[word] = freq_in_engaged * idf(word)

    if not interest_profile:
        return query_db(fallback_query)

    #  5. Score each candidate by how much it overlaps with the interest profile 
    scored = []
    for post in candidates:
        if post['id'] in engaged_post_ids:
            continue  # Already seen/engaged with, don't recommend it again.
        tokens = set(_tokenize(post['content']))
        if not tokens:
            continue
        overlap_score = sum(interest_profile.get(tok, 0.0) for tok in tokens)
        if overlap_score > 0:
            scored.append((overlap_score, post))

    if not scored:
        return query_db(fallback_query)

    #  6. Take the 5 most relevant, then present them newest-first 
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_posts = [post for _, post in scored[:5]]
    top_posts.sort(key=lambda post: post['created_at'], reverse=True)

    return top_posts


if __name__ == '__main__':
    app.run(debug=True, port=8080)


