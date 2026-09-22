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
