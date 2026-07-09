CREATE DATABASE bluecore;
-- Grant to the role running this init script (the container's POSTGRES_USER),
-- so it works for any username, not just "airflow". Using a literal role name
GRANT ALL PRIVILEGES ON DATABASE bluecore TO CURRENT_USER;
