# Python Engineering Style Guide & Standards

## 1. Naming Conventions and Type Annotations
Functions and variables must use snake_case. Classes must use PascalCase. Constants must use UPPER_CASE.
All public function signatures should include type annotations for arguments and return types to ensure type safety and readability.

## 2. Function Arguments and Mutable Defaults
Never use mutable objects (such as lists `[]`, dictionaries `{}`, or sets) as default argument values. Default arguments are evaluated once at module load time, causing state to persist across calls. Always use `None` as the default and initialize the mutable object inside the function body (`if arg is None: arg = []`).

## 3. Safe Database Queries and SQL Injection Prevention
Never concatenate user input directly into SQL strings using f-strings, `%`, or `+`. Always use parameterized queries or ORM placeholders (e.g. `cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))`).

## 4. Exception Handling and Swallowing Errors
Avoid bare `except:` clauses. Always catch specific exception classes (e.g., `except ValueError:`, `except KeyError:`). Never silently ignore errors with `pass` unless explicitly justified with a comment and debug logging.

## 5. Resource Management and File Handling
Always manage external resources, file descriptors, network sockets, and database connections using context managers (`with` statements). Never leave connections or file handles unclosed.

## 6. Secure Secrets and Credentials Handling
API keys, tokens, passwords, and sensitive credentials must never be hardcoded into source files. Retrieve credentials securely from environment variables using `os.getenv()` or secret management vaults.
