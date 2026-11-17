# SpyderMUSIC — modular Qt6 launcher
import multiprocessing

if __name__ == "__main__":
    # PyInstaller: divert --multiprocessing-fork workers before loading the GUI/engine.
    multiprocessing.freeze_support()

    # In source mode and as an additional safeguard alongside the PyInstaller hook.
    from music_package_bootstrap import configure_music_package_paths
    configure_music_package_paths()

    from spyder_engine import setup_database
    from spyder_gui import start_qt6_full_gui

    setup_database()
    raise SystemExit(start_qt6_full_gui())
