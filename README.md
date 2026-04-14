**agent-cli** is a one-shot command-line helper that has scoped execution tasks, runs in the foreground, shows you what it's doing, uses isolation and unix-style primitives, and supports things like macros.

Here's some differences:

The path it uses for the tools it has access to are always observable symlinks in a config directory (defaults to .config/agent-cli/tools/{task}/bin). These start out with a very minimal set of things: whatis, apropos, man, pydoc in doc/bin, cat, head, tail and ls in find/bin. The agent will use these to find out what's installed on the system or what it needs to install. 

Each of these tools get classified by task.

When the agent makes the plan about how to achieve its goal it uses these to try to execute it upon it. Each of these are stored in the agent-cli/skills directory and given a name. These are identical to Anthropic sklils and are completely substitutable

Beyond this, the agent always has two choices:
    
    * write a program (stored in tools/)
    * try to do the task without writing a tool

Regardless if first sees if it has an appropriate skill it can leverage. If so it tries to use it.

## Agent flow

In order to do a task here is what happens internally:

    1. The harness first looks at the current context in the current directory and creates a success condition for the task. This will allow it to make sure it's succeeded.

    2. It then looks at the current skills and sees if any apply.

    3. If so, it tries to apply the skill and see if it passes the test.

    4. Otherwise it tries to come up with a plan that uses the current tools.

    5. Before executing the plan it steps through it piece by piece trying to assess what happens. After step 1, X happens. Does this work us towards our goal, does this allow for the next step to happen? Is this destructive? 

    6. If it passes these tests then, if it needs more tools it will ask the user to allow or install them. If not it will try to do the task.

As a result you can do things like "clone these repositories: x, y, z" or "bump the z version number and do a new release"

## Model key configuration

agent-cli is model and provider agnostic. The configuration has a triplet, base_url, model, and key. These are all overridable on the command line and can be set via a set command like so:

$ agent-cli --set model "model-name"

Or like so for one-shot
$ agent-cli --model "model-name" "task to do"
