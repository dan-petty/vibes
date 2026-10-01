# Chapter 3: Keep It Simple (Complexity Caps & No Spaghetti)

> **TLDR**: Complex, deeply nested code causes both human developers and AI models to lose context; capping function complexity and nesting depth prevents unmaintainable spaghetti.
>
> **ELI:7b**: Don't write giant monster functions with ten nested `if` statements inside loops. Keep functions short, clear, and split into tiny helpers that do one job well.

---

## What is "Complexity" Without the Jargon?

In software engineering papers, you will see phrases like:

> *"McCabe Cyclomatic Complexity $M \le 10$ across all Abstract Syntax Tree (AST) subtrees."*

That sounds like rocket science. But in plain English, **cyclomatic complexity is simply counting how many forks in the road exist in a function**.

Every time you write:
- `if`
- `elif`
- `for`
- `while`
- `try` / `except`
- `and` / `or`

You are creating a fork in the road.

If a function has 1 fork, it's easy to read. If a function has 15 forks, there are $2^{15}$ (over 32,000) possible paths through that function!

Neither a human developer nor an AI model can hold 32,000 execution paths in their head at the same time. The AI will inevitably forget one of the branches, mishandle an error, or introduce a subtle regression.

---

## The Indentation "Arrowhead" Anti-Pattern

Have you ever opened a file and seen code that looks like this?

```python
# The Dreaded Arrowhead of Doom
def process_order(order):
    if order is not None:
        if order.is_valid:
            if order.has_items():
                for item in order.items:
                    if item.in_stock:
                        if user.has_funds(item.price):
                            # Finally doing the actual work!
                            charge_user(user, item)
```

Look at that code. It slopes diagonally across the screen like an arrowhead pointing to the right (`>>>>>>`).

This is called **deep nesting**. Every time code indents further to the right, your mental overhead doubles:
- What condition was that second `if` checking again?
- What happens if the fifth `if` fails? Where does the `else` go?
- Did we close all the brackets?

When an AI tries to edit an arrowhead function, it regularly gets confused about which block it is in, leading to indentation errors and lost logic.

---

## The Two Simple Rules: 10 Forks, 4 Indents

In high-reliability engineering (like `devops-cli` and `vibes`), we enforce two golden numerical limits:

1. **Max 10 Forks in Any Function**: If a function has more than 10 decisions (`if`, `for`, `try`, etc.), it is too big. Stop and break it up.
2. **Max 4 Levels of Indentation**: If code indents more than 4 times, you must flatten it.

---

## How to Flatten Code in 3 Easy Steps

You don't need fancy math to make code simple. Here are the three practical techniques you or your AI can use:

### 1. Guard Clauses (Return Early!)

Instead of wrapping the entire function in an `if`, check for bad cases first and exit immediately:

```python
# BEFORE (Arrowhead):
def send_email(user, message):
    if user is not None:
        if user.is_active:
            if message.is_valid():
                smtp_send(user.email, message)

# AFTER (Flat & Friendly):
def send_email(user, message):
    if user is None or not user.is_active:
        return
    if not message.is_valid():
        return

    smtp_send(user.email, message)
```

Notice how clean the second version is. The "bad" cases are kicked out right at the door, and the happy path stays completely flat.

### 2. Table Dispatch (Use Dictionaries, Not 10 `if/elif` Blocks)

If you have a ladder of `if/elif` statements checking a string, replace it with a dictionary:

```python
# BEFORE (10-branch ladder):
def get_discount(tier):
    if tier == "bronze":
        return 0.05
    elif tier == "silver":
        return 0.10
    elif tier == "gold":
        return 0.20
    elif tier == "platinum":
        return 0.30
    return 0.0

# AFTER (Clean dictionary lookup):
DISCOUNTS = {
    "bronze": 0.05,
    "silver": 0.10,
    "gold": 0.20,
    "platinum": 0.30,
}

def get_discount(tier):
    return DISCOUNTS.get(tier, 0.0)
```

The dictionary version has **zero branching complexity**. It is impossible to mess up the branching logic because there isn't any!

### 3. Extract Small Helper Functions

If a function is doing three distinct tasks (e.g. validating input, calculating totals, and formatting an email), don't write one 60-line function. Write three 15-line functions with descriptive names:

- `validate_input(data)`
- `calculate_totals(items)`
- `format_confirmation(total)`

And then combine them in one main coordinator function that reads like plain English:

```python
def checkout(data):
    clean_data = validate_input(data)
    total = calculate_totals(clean_data.items)
    return format_confirmation(total)
```

---

## Why AIs Love Short Functions

When an AI is asked to fix a bug in a 15-line function:
- It can read the entire function in 1 second.
- It understands all variables and paths.
- It generates an exact, accurate patch with zero hallucination.

When an AI is asked to fix a bug in a 300-line monster function:
- It runs out of focus.
- It accidentally deletes unrelated code while trying to edit lines 150-180.
- It introduces regressions.

Keep your functions small, and your AI assistant will feel like a genius.

---

## Next Steps

Now you know how to write short, simple code. But what happens when you refactor or replace an old feature? What do you do with the old code?

➡️ [**Chapter 4: No Zombie Code (Delete Dead Code on Sight)**](./04-no-zombie-code.md)
