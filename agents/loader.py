'''
Loads an agent's Python file from its folder.

Agent folders are named with dashes, like their instructions (assesmentAgent/in-agent/in-agent.md), and
Python cannot `import` from a folder whose name has a dash. So code agents are loaded by file path:

    from agents.loader import load_agent
    in_agent = load_agent('assesmentAgent/in-agent/in_agent.py')
    agent = in_agent.InAgent.for_run(run_id)

Each file is loaded once and shared (registered in sys.modules under a name built from its path).
'''
import importlib.util, pathlib, sys

AGENTS_DIR = pathlib.Path(__file__).resolve().parent


def load_agent(relative_path):
    path = (AGENTS_DIR / relative_path).resolve()
    name = 'agents._loaded.' + '.'.join(p.replace('-', '_') for p in path.relative_to(AGENTS_DIR).with_suffix('').parts)
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return sys.modules[name]
