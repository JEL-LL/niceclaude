"""niceclaude -- pace Claude Code background work against its own usage windows.

There is deliberately no __version__ here. The version is stored in exactly one
place, pyproject.toml, and read back from the installed distribution's metadata
by cli.installed_version(). A literal in this file would be a second copy of the
number with nothing keeping the two in agreement -- publish.yml checks the git
tag against pyproject.toml and would not notice this one drifting.

Reading it here instead is not an option either. importlib.metadata walks
sys.path looking for dist-info, and this module runs on the hot path: the
niceclaude-hook entry point imports niceclaude.hook, which executes this file
first, on every single tool call.
"""
