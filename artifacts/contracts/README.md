# Application Contracts

Input to [`tools/app_factory.py`](../../tools/app_factory.py). A contract declares what an
application exposes — its operations, their arguments and their types — and the factory
emits a runnable application that enforces exactly that.

The contract is the source of truth for three artefacts that usually drift apart: the
dispatch table, the JSON Schema the arguments are validated against, and the tests that
assert undeclared arguments are refused. Generating all three from one declaration is the
point; hand-written, they agree until the first time someone adds a parameter.

## Shape

```yaml
name: Invoice Reconciler
slug: invoice-reconciler
module: reconciler
purpose: Reconcile submitted invoices against recorded payments and report discrepancies.
operations:
  - name: reconcile
    summary: Compare one invoice against recorded payments.
    arguments:
      - name: invoice_id
        type: string
        description: Identifier of the invoice under reconciliation.
      - name: tolerance
        type: number
        required: false
        description: Absolute difference tolerated before a discrepancy is reported.
```

Every argument is required unless it says otherwise. Types are JSON Schema primitives, so
the emitted schema and the emitted runtime check cannot disagree about what they mean.

## Variables

A contract may declare variables, and any text in it may reference them as `{{ name }}`:

```yaml
variables:
  - name: entity
    prompt: What does this application reconcile
    type: string
    default: invoice
  - name: tolerance_unit
    prompt: Unit the tolerance argument is expressed in
    type: choice
    choices: [currency, percent]
    default: currency

purpose: Reconcile submitted {{ entity }}s against recorded payments.
```

Answers come from `--set NAME=VALUE`, from `--answers <file>`, or from a person at a
terminal. Each generation records what it was told in `.factory-answers.yaml` beside the
application, so a regeneration replays the dialogue instead of repeating it:

```bash
python3 tools/app_factory.py new --contract invoice-reconciler.yaml --set entity=shipment
python3 tools/app_factory.py new --contract invoice-reconciler.yaml \
  --answers invoice-reconciler/.factory-answers.yaml
```

Two rules govern the rest.

**Every variable declares a default.** `cookiecutter.json` requires one for the same
reason: it is what makes a contract resolvable with nobody watching. Prompting is always an
override, never a dependency, and the factory never prompts when stdin is not a terminal —
a generator that asks a pipe a question does not fail, it waits.

**Answers are substituted after parsing, never before.** Rendering the YAML text and then
parsing the result would let an answer containing a colon or a newline introduce a key.
Substitution walks the parsed document and rewrites string leaves only, so the worst an
answer can do is be a longer string. It is a single pass, so an answer that itself contains
`{{ other }}` is inert text rather than a second expansion.
