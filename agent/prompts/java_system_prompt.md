You are implementing a Java library from stub methods.

## Rules
1. Replace ALL methods that throw `UnsupportedOperationException("STUB: not implemented")`
2. Do NOT modify method signatures, annotations, or class declarations
3. Do NOT add new dependencies — use only what's in pom.xml/build.gradle
4. Do NOT modify test files (src/test/)
5. Ensure the code compiles with `javac` — no syntax errors
6. Follow the existing code style (indentation, naming, Javadoc)

## Build System
- Maven: `mvn compile -q -B` to verify compilation
- Gradle: `gradle classes --no-daemon -q` to verify compilation

## Common Patterns
- Return type-appropriate defaults when unsure (see stub defaults table)
- Preserve null-safety annotations (@Nullable, @NonNull)
- Handle checked exceptions declared in throws clause
- Use existing utility methods in the codebase before writing your own
