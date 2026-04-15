from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="agent-cli",
    version="0.1.0",
    author="agent-cli contributors",
    description="A one-shot command-line helper with scoped execution tasks",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/example/agent-cli",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    py_modules=["agent_cli"],
    entry_points={
        "console_scripts": [
            "agent-cli=agent_cli:main",
            "ac=agent_cli:main",
        ],
    },
)
