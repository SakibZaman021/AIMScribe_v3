"""
Run database migration to convert session_id from UUID to VARCHAR(255)
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
import psycopg2

def run_migration():
    print("Connecting to PostgreSQL...")
    print(f"Host: {settings.postgres_host}")
    print(f"Database: {settings.postgres_db}")

    conn = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
        sslmode=getattr(settings, 'postgres_sslmode', 'prefer')
    )
    conn.autocommit = True

    def table_exists(cur, table_name):
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = %s
            )
        """, (table_name,))
        return cur.fetchone()[0]

    def get_column_type(cur, table_name, column_name):
        cur.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, (table_name, column_name))
        result = cur.fetchone()
        return result[0] if result else None

    def get_foreign_keys_to_sessions(cur):
        """Find all foreign keys referencing sessions table"""
        cur.execute("""
            SELECT
                tc.table_name,
                tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
                AND ccu.table_name = 'sessions'
        """)
        return cur.fetchall()

    def get_tables_with_session_id(cur):
        """Find all tables with session_id column"""
        cur.execute("""
            SELECT table_name, data_type FROM information_schema.columns
            WHERE column_name = 'session_id'
            ORDER BY table_name
        """)
        return cur.fetchall()

    try:
        with conn.cursor() as cur:
            # Check current state
            print("\nChecking current schema...")
            session_type = get_column_type(cur, 'sessions', 'session_id')
            print(f"  sessions.session_id type: {session_type}")

            if session_type == 'character varying':
                print("\n  Already VARCHAR - checking other tables...")
            elif session_type == 'uuid':
                print("\n  Currently UUID - will convert to VARCHAR(255)")

            # Find all tables with session_id
            print("\nTables with session_id column:")
            tables = get_tables_with_session_id(cur)
            for table, dtype in tables:
                print(f"  {table}: {dtype}")

            # Find all FK constraints to sessions
            print("\nFinding foreign key constraints to sessions...")
            fks = get_foreign_keys_to_sessions(cur)
            for table, constraint in fks:
                print(f"  {table}.{constraint}")

            # Step 1: Drop ALL foreign keys to sessions
            print("\nDropping ALL foreign key constraints to sessions...")
            for table, constraint in fks:
                try:
                    cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
                    print(f"  Dropped: {table}.{constraint}")
                except Exception as e:
                    print(f"  Error dropping {table}.{constraint}: {e}")

            # Step 2: Convert sessions.session_id first (parent table)
            print("\nConverting sessions.session_id to VARCHAR(255)...")
            if get_column_type(cur, 'sessions', 'session_id') == 'uuid':
                try:
                    cur.execute("ALTER TABLE sessions ALTER COLUMN session_id TYPE VARCHAR(255) USING session_id::text")
                    print("  sessions: converted to VARCHAR(255)")
                except Exception as e:
                    print(f"  sessions: {e}")
            else:
                print("  sessions: already VARCHAR")

            # Step 3: Convert all child tables
            print("\nConverting child tables session_id to VARCHAR(255)...")
            for table, dtype in tables:
                if table == 'sessions':
                    continue
                if dtype != 'character varying':
                    try:
                        cur.execute(f"ALTER TABLE {table} ALTER COLUMN session_id TYPE VARCHAR(255) USING session_id::text")
                        print(f"  {table}: converted to VARCHAR(255)")
                    except Exception as e:
                        print(f"  {table}: {e}")
                else:
                    print(f"  {table}: already VARCHAR")

            # Step 4: Add new columns
            print("\nAdding new columns...")
            try:
                cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ner_webhook_url TEXT")
                cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS status_webhook_url TEXT")
                print("  sessions: added webhook columns")
            except Exception as e:
                print(f"  sessions webhook columns: {e}")

            try:
                cur.execute("ALTER TABLE clips ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64)")
                print("  clips: added idempotency_key")
            except Exception as e:
                print(f"  clips idempotency_key: {e}")

            # Step 5: Re-add foreign keys
            print("\nRe-adding foreign key constraints...")
            for table, constraint in fks:
                if table_exists(cur, table):
                    try:
                        cur.execute(f"""
                            ALTER TABLE {table} ADD CONSTRAINT {constraint}
                            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                        """)
                        print(f"  {table}: added FK")
                    except Exception as e:
                        if "already exists" in str(e):
                            print(f"  {table}: FK already exists")
                        else:
                            print(f"  {table}: {e}")

            # Verify
            print("\nVerification:")
            for table, _ in tables:
                col_type = get_column_type(cur, table, 'session_id')
                print(f"  {table}.session_id: {col_type}")

        print("\nMigration completed!")

    except Exception as e:
        print(f"\nMigration error: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    run_migration()
