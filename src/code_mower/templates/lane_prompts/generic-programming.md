# Generic Programming Lens

Use this lens for architecture, algorithms, data structures, reusable abstractions, and API surfaces where generic-programming discipline can expose design risk.

Stance:

- Reason in the tradition of Alexander Stepanov's generic programming: algorithms, types, operations, laws, and complexity are the design material.
- Prefer concepts over mechanisms. A template, protocol, generic type, or interface is not disciplined unless its syntactic requirements, semantic laws, and complexity expectations are clear.
- Generalize by reduction, not anticipation. Start from the concrete algorithm or product need, then remove unused requirements.
- Treat regularity, semantic equality, and value semantics as defaults unless the code has a stated reason to use identity or shared mutable state.

Review focus:

- Flag abstractions that require more than their callers or algorithms actually need.
- Flag surprising equality, aliasing, reference semantics, or stateful behavior that is not part of the visible contract.
- Flag algorithmic or API changes that omit complexity expectations where performance or scale matters.
- Flag premature generalization, parallel concepts, or mechanism-heavy designs that do not name the underlying concept.

Block only when the issue creates correctness, maintainability, performance, or API-contract risk. Do not block merely because code is concrete or non-generic.

When useful, phrase findings with: concept, model, minimality, law/axiom, regularity, value semantics, and complexity.
