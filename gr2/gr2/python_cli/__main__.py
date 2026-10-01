from .app import main

if __name__ == "__main__":
    # `main()`, never `app()`: the AdapterError boundary lives on main(), and this
    # module is a second way in. Calling app() here left a door with no boundary on
    # it, so `python -m gr2.python_cli` — an invocation our own documentation uses —
    # handed a first-run reader a traceback while the console script did not.
    main()
