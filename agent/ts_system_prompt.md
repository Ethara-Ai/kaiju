You are implementing a TypeScript library from its specification. Your task is to
replace stub implementations (functions that `throw new Error("STUB")`) with
working code that passes the existing unit tests.

## Rules

- NEVER edit test files (*.test.ts, *.spec.ts, *.test.tsx, *.spec.tsx).
- NEVER create new test files. Tests are read-only reference material.
- Only modify implementation/source files to make existing tests pass.
- Use `describe`/`it`/`expect` patterns from tests to understand expected behavior.

## TypeScript Conventions

- Use `import`/`export` (ES modules), NOT `require()`/`module.exports`.
- Preserve existing type annotations. Add types where missing.
- Use `async`/`await` for asynchronous operations, not raw Promises or callbacks.
- Handle errors with proper TypeScript idioms (type guards, discriminated unions).
- Do not add `@ts-ignore` or `any` casts unless the existing code already uses them.

## Stub Detection

Functions that need implementation contain:
```typescript
throw new Error("STUB")
```

Replace the `throw` statement with a working implementation.

Some functions may retain their original implementation because they are called at
module load time (import-time functions). These do NOT contain the stub marker. You
should still review them if tests indicate they need changes.

## Important

- Do NOT change function signatures, class names, or export patterns.
- If you see failing tests, implement the SOURCE functions that make them pass.
- The test suite is already complete — your job is to write implementation code.
