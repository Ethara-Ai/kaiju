/**
 * AST-based TypeScript code stubbing engine using ts-morph.
 *
 * Replaces non-import-time function bodies with `throw new Error("STUB")`
 * while preserving functions required for module initialization.
 *
 * Two-pass approach:
 *   Pass 1 — Collect import-time function names from source + extra scan dirs,
 *            then resolve transitively via call graph (fixed-point, max 10 iterations).
 *   Pass 2 — Stub all non-import-time functions. Import-time functions get safe
 *            return values (`return {} as T`, `return ""`, etc.).
 *
 * Outputs JSON report to stdout, diagnostics to stderr.
 *
 * Usage:
 *   npx ts-node tools/stub_ts.ts --src-dir X --extra-scan-dirs Y --mode all --verbose
 */

import {
  Project,
  SourceFile,
  Node,
  SyntaxKind,
  FunctionDeclaration,
  MethodDeclaration,
  ConstructorDeclaration,
  GetAccessorDeclaration,
  SetAccessorDeclaration,
  ArrowFunction,
  FunctionExpression,
  ClassDeclaration,
  ClassStaticBlockDeclaration,
} from "ts-morph";
import * as path from "path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface StubReport {
  files_processed: number;
  files_modified: number;
  functions_stubbed: number;
  functions_preserved: number;
  import_time_names: string[];
  errors: Array<{ file: string; error: string }>;
}

interface CliArgs {
  srcDir: string;
  extraScanDirs: string[];
  mode: "all" | "docstring" | "combined";
  verbose: boolean;
}

type StubbableNode =
  | FunctionDeclaration
  | MethodDeclaration
  | ConstructorDeclaration
  | GetAccessorDeclaration
  | SetAccessorDeclaration
  | ArrowFunction
  | FunctionExpression;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BUILTINS_TO_IGNORE = new Set<string>([
  "console", "Math", "JSON", "Object", "Array", "String", "Number", "Boolean",
  "Promise", "Map", "Set", "WeakMap", "WeakSet", "Error", "TypeError",
  "RangeError", "Date", "RegExp", "Symbol", "parseInt", "parseFloat", "isNaN",
  "isFinite", "undefined", "NaN", "Infinity", "require", "module", "exports",
  "process", "Buffer", "setTimeout", "setInterval", "clearTimeout",
  "clearInterval", "queueMicrotask",
]);

const TEST_FILE_PATTERNS = [
  /\.test\.tsx?$/,
  /\.spec\.tsx?$/,
  /__tests__\//,
  /\/test\//,
  /\/tests\//,
  /\.stories\.tsx?$/,
  /\/__mocks__\//,
  /\/mocks\//,
  /\/testing\//,
  /\/fixtures\//,
];

const SKIP_METHODS = new Set<string>([
  "toString", "valueOf",
  "[Symbol.toPrimitive]", "[Symbol.iterator]", "[Symbol.asyncIterator]",
  "[Symbol.hasInstance]", "[Symbol.toStringTag]",
]);

const MAX_TRANSITIVE_ITERATIONS = 10;

// ---------------------------------------------------------------------------
// CLI parsing
// ---------------------------------------------------------------------------

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {
    srcDir: "",
    extraScanDirs: [],
    mode: "all",
    verbose: false,
  };

  for (let i = 2; i < argv.length; i++) {
    switch (argv[i]) {
      case "--src-dir":
        args.srcDir = argv[++i];
        break;
      case "--extra-scan-dirs":
        args.extraScanDirs = argv[++i].split(",").map((d) => d.trim()).filter(Boolean);
        break;
      case "--mode":
        args.mode = argv[++i] as CliArgs["mode"];
        break;
      case "--verbose":
        args.verbose = true;
        break;
      default:
        log(`Unknown argument: ${argv[i]}`);
    }
  }

  if (!args.srcDir) {
    log("Error: --src-dir is required");
    process.exit(1);
  }

  return args;
}

// ---------------------------------------------------------------------------
// Logging (all diagnostics to stderr)
// ---------------------------------------------------------------------------

function log(msg: string): void {
  process.stderr.write(msg + "\n");
}

function logVerbose(verbose: boolean, msg: string): void {
  if (verbose) log(msg);
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function isTestFile(filePath: string): boolean {
  return TEST_FILE_PATTERNS.some((p) => p.test(filePath));
}

function extractCallName(callText: string): string | undefined {
  const trimmed = callText.trim();
  if (/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(trimmed)) {
    return trimmed;
  }
  const match = trimmed.match(/(?:^|\.)\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:<[^>]*>)?\s*\(/)
    ?? trimmed.match(/\.([A-Za-z_$][A-Za-z0-9_$]*)$/);
  return match?.[1];
}

function collectCallNames(node: Node): Set<string> {
  const names = new Set<string>();
  for (const call of node.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    const expr = call.getExpression();
    const name = extractCallName(expr.getText());
    if (name && !BUILTINS_TO_IGNORE.has(name)) {
      names.add(name);
    }
  }
  return names;
}

function isAmbient(node: Node): boolean {
  let current: Node | undefined = node;
  while (current) {
    if (Node.isModifierable(current)) {
      if (current.hasModifier(SyntaxKind.DeclareKeyword)) return true;
    }
    current = current.getParent();
  }
  return false;
}

/**
 * Normalize return type text: strip `import("./path").` prefixes.
 * e.g. `import("/foo/bar").MyType` -> `MyType`
 */
function getReturnTypeText(node: StubbableNode): string {
  try {
    if (Node.isConstructorDeclaration(node)) {
      return "void";
    }
    const sig = node.getSignature();
    const retType = sig.getReturnType();
    let typeText = retType.getText(node);
    typeText = typeText.replace(/import\("[^"]*"\)\./g, "");
    return typeText;
  } catch {
    return "any";
  }
}

/**
 * Build a safe return statement for import-time functions.
 * Instead of throwing, these return a type-appropriate default.
 */
function buildSafeReturn(node: StubbableNode): string {
  const typeText = getReturnTypeText(node);
  return buildSafeReturnFromType(typeText);
}

function buildSafeReturnFromType(typeText: string): string {
  const t = typeText.trim();

  if (t === "void" || t === "never") {
    return "// import-time preserved";
  }
  if (t === "string") {
    return 'return "";';
  }
  if (t === "number") {
    return "return 0;";
  }
  if (t === "boolean") {
    return "return false;";
  }
  if (t === "null") {
    return "return null;";
  }
  if (t === "undefined") {
    return "return undefined;";
  }
  if (t.endsWith("[]") || /^Array</.test(t)) {
    return "return [];";
  }
  const promiseMatch = t.match(/^Promise<(.+)>$/);
  if (promiseMatch) {
    const inner = buildSafeReturnFromType(promiseMatch[1]);
    if (inner.startsWith("//")) {
      return "return Promise.resolve(undefined as any);";
    }
    const valMatch = inner.match(/^return\s+(.+);$/);
    if (valMatch) {
      return `return Promise.resolve(${valMatch[1]});`;
    }
    return `return Promise.resolve({} as any);`;
  }
  return `return {} as ${t};`;
}

// ---------------------------------------------------------------------------
// Pass 1: Collect import-time function names
// ---------------------------------------------------------------------------

/**
 * Scan source files and collect names of functions called at import time.
 *
 * Detected patterns:
 *  1. Top-level expression calls: `register()`
 *  2. Variable declarations with call initializers: `const x = func()`
 *  3. Decorator factories: `@Dec(args)`
 *  4. Static property initializers: `static x = func()`
 *  5. Static blocks: `static { init() }`
 *  6. Module-level if/try with calls
 *  7. Named imports: `import { Y } from 'X'`
 *  TS-1. Barrel re-exports: `export { X } from './mod'`
 *  TS-2. Top-level await: `await func()` in top-level
 *  TS-3. Module augmentation: `declare module` (skip body, add names)
 *  TS-4. Static property initializers (same as #4)
 *  TS-5. Enum member initializers with calls
 */
function collectImportTimeNames(sourceFiles: SourceFile[]): Set<string> {
  const names = new Set<string>();

  for (const sf of sourceFiles) {
    if (isTestFile(sf.getFilePath())) continue;

    for (const stmt of sf.getStatements()) {
      // Pattern 1: Top-level expression statements with calls
      if (Node.isExpressionStatement(stmt)) {
        const expr = stmt.getExpression();
        names.merge(collectCallNames(stmt));

        // TS-2: Top-level await expressions
        if (Node.isAwaitExpression(expr)) {
          names.merge(collectCallNames(expr));
        }
      }

      // Pattern 2: Variable declarations with call initializers
      if (Node.isVariableStatement(stmt)) {
        for (const decl of stmt.getDeclarations()) {
          const init = decl.getInitializer();
          if (init && Node.isCallExpression(init)) {
            const name = extractCallName(init.getExpression().getText());
            if (name && !BUILTINS_TO_IGNORE.has(name)) names.add(name);
          }
          if (init) {
            names.merge(collectCallNames(init));
          }
        }
      }

      // Pattern 3 & class patterns: decorators, static props, static blocks
      if (Node.isClassDeclaration(stmt)) {
        collectClassImportTimeNames(stmt, names);
      }

      // Pattern 6: Module-level if statements
      if (Node.isIfStatement(stmt)) {
        names.merge(collectCallNames(stmt));
      }

      // Pattern 6: Module-level try statements
      if (Node.isTryStatement(stmt)) {
        names.merge(collectCallNames(stmt));
      }

      if (Node.isExportDeclaration(stmt)) {
        if (stmt.getModuleSpecifier() === undefined) {
          const namedExports = stmt.getNamedExports();
          for (const ne of namedExports) {
            names.add(ne.getName());
            const alias = ne.getAliasNode();
            if (alias) names.add(alias.getText());
          }
        }
      }

      if (Node.isEnumDeclaration(stmt)) {
        for (const member of stmt.getMembers()) {
          const init = member.getInitializer();
          if (init) {
            names.merge(collectCallNames(init));
          }
        }
      }

      // TS-3: Module augmentation — skip body, just note it exists
      if (Node.isModuleDeclaration(stmt)) {
        names.merge(collectCallNames(stmt));
      }
    }
  }

  for (const b of BUILTINS_TO_IGNORE) {
    names.delete(b);
  }

  return names;
}

function collectClassImportTimeNames(
  classDecl: ClassDeclaration,
  names: Set<string>,
): void {
  for (const dec of classDecl.getDecorators()) {
    if (dec.isDecoratorFactory()) {
      names.merge(collectCallNames(dec));
    }
  }

  for (const method of classDecl.getMethods()) {
    for (const dec of method.getDecorators()) {
      if (dec.isDecoratorFactory()) {
        names.merge(collectCallNames(dec));
      }
    }
  }

  for (const prop of classDecl.getProperties()) {
    for (const dec of prop.getDecorators()) {
      if (dec.isDecoratorFactory()) {
        names.merge(collectCallNames(dec));
      }
    }
  }

  for (const prop of classDecl.getProperties()) {
    if (prop.isStatic()) {
      const init = prop.getInitializer();
      if (init) {
        names.merge(collectCallNames(init));
      }
    }
  }

  for (const member of classDecl.getMembers()) {
    if (Node.isClassStaticBlockDeclaration(member)) {
      names.merge(collectCallNames(member));
    }
  }

}

// ---------------------------------------------------------------------------
// Call graph & transitive resolution
// ---------------------------------------------------------------------------

interface CallGraph {
  /** function name -> set of names it calls */
  callees: Map<string, Set<string>>;
  /** all defined function/method names across the project */
  definedNames: Set<string>;
}

/**
 * Build a call graph from all source files.
 * Tracks: function declarations, method declarations, and variable-assigned
 * arrow functions / function expressions.
 */
function buildCallGraph(sourceFiles: SourceFile[]): CallGraph {
  const callees = new Map<string, Set<string>>();
  const definedNames = new Set<string>();

  function addCallees(name: string, body: Node | undefined): void {
    if (!body) return;
    definedNames.add(name);
    const calls = collectCallNames(body);
    const existing = callees.get(name);
    if (existing) {
      for (const c of calls) existing.add(c);
    } else {
      callees.set(name, calls);
    }
  }

  for (const sf of sourceFiles) {
    if (isTestFile(sf.getFilePath())) continue;

    for (const fn of sf.getDescendantsOfKind(SyntaxKind.FunctionDeclaration)) {
      const name = fn.getName();
      if (name) addCallees(name, fn.getBody());
    }

    for (const m of sf.getDescendantsOfKind(SyntaxKind.MethodDeclaration)) {
      const name = m.getName();
      addCallees(name, m.getBody());
    }

    for (const vd of sf.getDescendantsOfKind(SyntaxKind.VariableDeclaration)) {
      const init = vd.getInitializer();
      if (!init) continue;
      if (Node.isArrowFunction(init) || Node.isFunctionExpression(init)) {
        const name = vd.getName();
        addCallees(name, init.getBody());
      }
    }
  }

  return { callees, definedNames };
}

/**
 * Resolve transitive import-time names using fixed-point iteration.
 * If function A is import-time and A calls B, then B is also import-time
 * (provided B is defined in the scanned source).
 */
function resolveTransitive(
  importTimeNames: Set<string>,
  callGraph: CallGraph,
  verbose: boolean,
): Set<string> {
  const resolved = new Set(importTimeNames);

  for (let iteration = 0; iteration < MAX_TRANSITIVE_ITERATIONS; iteration++) {
    const before = resolved.size;
    const newNames = new Set<string>();

    for (const name of resolved) {
      const calls = callGraph.callees.get(name);
      if (!calls) continue;
      for (const callee of calls) {
        if (!resolved.has(callee) && callGraph.definedNames.has(callee) && !BUILTINS_TO_IGNORE.has(callee)) {
          newNames.add(callee);
        }
      }
    }

    for (const n of newNames) resolved.add(n);

    if (newNames.size > 0) {
      logVerbose(verbose, `  Transitive iteration ${iteration}: added ${newNames.size} names: ${[...newNames].slice(0, 10).join(", ")}`);
    }

    if (resolved.size === before) break;
  }

  return resolved;
}

// ---------------------------------------------------------------------------
// Skip conditions
// ---------------------------------------------------------------------------

/**
 * Check if a constructor only contains super() calls and this.x = y assignments.
 * Such constructors are structural and should be preserved.
 */
function isSimpleConstructor(ctor: ConstructorDeclaration): boolean {
  const body = ctor.getBody();
  if (!body || !Node.isBlock(body)) return true;

  for (const stmt of body.getStatements()) {
    if (Node.isExpressionStatement(stmt)) {
      const expr = stmt.getExpression();
      if (Node.isCallExpression(expr)) {
        const callee = expr.getExpression();
        if (callee.getText() === "super") continue;
      }
      if (Node.isBinaryExpression(expr)) {
        const left = expr.getLeft();
        if (Node.isPropertyAccessExpression(left) && left.getExpression().getText() === "this") {
          continue;
        }
      }
    }
    return false;
  }
  return true;
}

function isOverloadSignature(node: StubbableNode): boolean {
  if (Node.isFunctionDeclaration(node) || Node.isMethodDeclaration(node)) {
    return node.isOverload();
  }
  return false;
}

function isAlreadyStubbed(node: StubbableNode): boolean {
  try {
    if (Node.isArrowFunction(node) || Node.isFunctionExpression(node)) {
      const body = node.getBody();
      if (!body) return false;
      return body.getText().includes('"STUB"');
    }
    if ("hasBody" in node && typeof (node as any).hasBody === "function") {
      if (!(node as any).hasBody()) return false;
    }
    const body = (node as any).getBody?.();
    if (!body) return false;
    return body.getText().includes('"STUB"');
  } catch {
    return false;
  }
}

function isSkipMethod(node: StubbableNode): boolean {
  if (Node.isMethodDeclaration(node) || Node.isGetAccessorDeclaration(node) || Node.isSetAccessorDeclaration(node)) {
    const name = node.getName();
    return SKIP_METHODS.has(name);
  }
  return false;
}

function getNodeName(node: StubbableNode): string {
  if (Node.isConstructorDeclaration(node)) return "constructor";
  if (Node.isArrowFunction(node) || Node.isFunctionExpression(node)) {
    const parent = node.getParent();
    if (parent && Node.isVariableDeclaration(parent)) {
      return parent.getName();
    }
    if (parent && Node.isPropertyDeclaration(parent)) {
      return parent.getName();
    }
    return "(anonymous)";
  }
  return (node as any).getName?.() ?? "(anonymous)";
}

function shouldSkip(node: StubbableNode): boolean {
  if (isAmbient(node)) return true;
  if (isOverloadSignature(node)) return true;
  if (isAlreadyStubbed(node)) return true;

  if (Node.isArrowFunction(node) || Node.isFunctionExpression(node)) {
  } else {
    if ("hasBody" in node && typeof (node as any).hasBody === "function") {
      if (!(node as any).hasBody()) return true;
    }
    if (Node.isMethodDeclaration(node) && node.isAbstract()) return true;
  }

  if (Node.isConstructorDeclaration(node) && isSimpleConstructor(node)) return true;
  if (isSkipMethod(node)) return true;

  return false;
}

// ---------------------------------------------------------------------------
// Pass 2: Stub functions
// ---------------------------------------------------------------------------

function stubSourceFile(
  sf: SourceFile,
  importTimeNames: Set<string>,
  report: StubReport,
  verbose: boolean,
): boolean {
  const filePath = sf.getFilePath();
  if (isTestFile(filePath)) return false;

  let modified = false;
  const nodesToStub: Array<{ node: StubbableNode; isImportTime: boolean }> = [];

  const allNodes: StubbableNode[] = [];
  allNodes.push(...sf.getDescendantsOfKind(SyntaxKind.FunctionDeclaration));
  allNodes.push(...sf.getDescendantsOfKind(SyntaxKind.MethodDeclaration));
  allNodes.push(...sf.getDescendantsOfKind(SyntaxKind.Constructor));
  allNodes.push(...sf.getDescendantsOfKind(SyntaxKind.GetAccessor));
  allNodes.push(...sf.getDescendantsOfKind(SyntaxKind.SetAccessor));
  allNodes.push(...sf.getDescendantsOfKind(SyntaxKind.ArrowFunction));
  allNodes.push(...sf.getDescendantsOfKind(SyntaxKind.FunctionExpression));

  for (const node of allNodes) {
    if (shouldSkip(node)) continue;

    const name = getNodeName(node);
    const isImportTime = importTimeNames.has(name);
    nodesToStub.push({ node, isImportTime });
  }

  nodesToStub.sort((a, b) => b.node.getStart() - a.node.getStart());

  for (const { node, isImportTime } of nodesToStub) {
    const name = getNodeName(node);
    try {
      if (isImportTime) {
        // Match Python behavior: SKIP import-time functions entirely.
        // Original body is preserved — no safe return replacement.
        // This avoids the "dual stub marker" problem where has_ts_stubs()
        // can't detect import-time safe returns like `return false;`.
        report.functions_preserved++;
        logVerbose(verbose, `  [SKIP] ${path.relative(process.cwd(), filePath)}::${name} (import-time, body preserved)`);
        continue;
      } else {
        stubFunction(node);
        report.functions_stubbed++;
        logVerbose(verbose, `  [STUB] ${path.relative(process.cwd(), filePath)}::${name}`);
      }
      modified = true;
    } catch (e: any) {
      const errMsg = e?.message ?? String(e);
      report.errors.push({ file: filePath, error: `${name}: ${errMsg}` });
      log(`  [ERR]  ${filePath}::${name} — ${errMsg}`);
    }
  }

  return modified;
}

function stubFunction(node: StubbableNode): void {
  const stubBody = 'throw new Error("STUB");';

  if (Node.isArrowFunction(node)) {
    const body = node.getBody();
    if (body && !Node.isBlock(body)) {
      body.replaceWithText(`{ ${stubBody} }`);
    } else {
      node.setBodyText(stubBody);
    }
    return;
  }

  if (Node.isFunctionExpression(node)) {
    node.setBodyText(stubBody);
    return;
  }

  if (Node.isSetAccessorDeclaration(node)) {
    node.setBodyText(stubBody);
    return;
  }

  (node as FunctionDeclaration | MethodDeclaration | ConstructorDeclaration | GetAccessorDeclaration)
    .setBodyText(stubBody);
}

function stubImportTimeFunction(node: StubbableNode): void {
  const safeReturn = buildSafeReturn(node);

  if (Node.isArrowFunction(node)) {
    const body = node.getBody();
    if (body && !Node.isBlock(body)) {
      body.replaceWithText(`{ ${safeReturn} }`);
    } else {
      node.setBodyText(safeReturn);
    }
    return;
  }

  if (Node.isFunctionExpression(node)) {
    node.setBodyText(safeReturn);
    return;
  }

  if (Node.isSetAccessorDeclaration(node)) {
    node.setBodyText("// import-time preserved");
    return;
  }

  if (Node.isConstructorDeclaration(node)) {
    node.setBodyText("// import-time preserved");
    return;
  }

  (node as FunctionDeclaration | MethodDeclaration | GetAccessorDeclaration)
    .setBodyText(safeReturn);
}

// ---------------------------------------------------------------------------
// Set.prototype.merge polyfill
// ---------------------------------------------------------------------------

declare global {
  interface Set<T> {
    merge(other: Set<T>): Set<T>;
  }
}

Set.prototype.merge = function <T>(this: Set<T>, other: Set<T>): Set<T> {
  for (const item of other) {
    this.add(item);
  }
  return this;
};

// ---------------------------------------------------------------------------
// Project setup
// ---------------------------------------------------------------------------

function createProject(srcDir: string, extraScanDirs: string[]): Project {
  const project = new Project({
    compilerOptions: {
      allowJs: true,
      checkJs: false,
      noEmit: true,
      strict: false,
      skipLibCheck: true,
      esModuleInterop: true,
      resolveJsonModule: true,
      target: 99,
      module: 99,
    },
    skipAddingFilesFromTsConfig: true,
  });

  project.addSourceFilesAtPaths([
    path.join(srcDir, "**/*.ts"),
    path.join(srcDir, "**/*.tsx"),
  ]);

  for (const dir of extraScanDirs) {
    project.addSourceFilesAtPaths([
      path.join(dir, "**/*.ts"),
      path.join(dir, "**/*.tsx"),
    ]);
  }

  return project;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function main(): void {
  const args = parseArgs(process.argv);

  const srcDirAbs = path.resolve(args.srcDir);
  const extraDirsAbs = args.extraScanDirs.map((d) => path.resolve(d));

  log(`stub_ts: src-dir=${srcDirAbs}, extra-scan-dirs=[${extraDirsAbs.join(", ")}], mode=${args.mode}`);

  const report: StubReport = {
    files_processed: 0,
    files_modified: 0,
    functions_stubbed: 0,
    functions_preserved: 0,
    import_time_names: [],
    errors: [],
  };

  let project: Project;
  try {
    project = createProject(srcDirAbs, extraDirsAbs);
  } catch (e: any) {
    log(`Error creating project: ${e?.message ?? e}`);
    report.errors.push({ file: "(project)", error: e?.message ?? String(e) });
    process.stdout.write(JSON.stringify(report, null, 2) + "\n");
    process.exit(1);
  }

  const allSourceFiles = project.getSourceFiles();
  log(`Loaded ${allSourceFiles.length} source files`);

  const srcFiles = allSourceFiles.filter((sf) => sf.getFilePath().startsWith(srcDirAbs));
  const allScanFiles = allSourceFiles;

  // -----------------------------------------------------------------------
  // Pass 1: Collect import-time names
  // -----------------------------------------------------------------------
  log("Pass 1: Collecting import-time function names...");
  const rawImportTimeNames = collectImportTimeNames(allScanFiles);
  logVerbose(args.verbose, `  Raw import-time names (${rawImportTimeNames.size}): ${[...rawImportTimeNames].slice(0, 20).join(", ")}`);

  const callGraph = buildCallGraph(allScanFiles);
  logVerbose(args.verbose, `  Call graph: ${callGraph.definedNames.size} defined functions, ${callGraph.callees.size} with call data`);

  const importTimeNames = resolveTransitive(rawImportTimeNames, callGraph, args.verbose);
  log(`Pass 1 complete: ${importTimeNames.size} import-time names (${rawImportTimeNames.size} direct + ${importTimeNames.size - rawImportTimeNames.size} transitive)`);

  report.import_time_names = [...importTimeNames].sort();

  // -----------------------------------------------------------------------
  // Pass 2: Stub functions
  // -----------------------------------------------------------------------
  log("Pass 2: Stubbing functions...");

  for (const sf of srcFiles) {
    const filePath = sf.getFilePath();
    if (isTestFile(filePath)) continue;

    report.files_processed++;

    try {
      const wasModified = stubSourceFile(sf, importTimeNames, report, args.verbose);
      if (wasModified) {
        report.files_modified++;
      }
    } catch (e: any) {
      const errMsg = e?.message ?? String(e);
      report.errors.push({ file: filePath, error: errMsg });
      log(`  [ERR] ${filePath}: ${errMsg}`);
    }
  }

  log("Saving...");
  try {
    project.saveSync();
  } catch (e: any) {
    log(`Error saving: ${e?.message ?? e}`);
    report.errors.push({ file: "(save)", error: e?.message ?? String(e) });
  }

  log(`\nDone. ${report.files_processed} processed, ${report.files_modified} modified, ${report.functions_stubbed} stubbed, ${report.functions_preserved} preserved`);
  if (report.errors.length > 0) {
    log(`  ${report.errors.length} error(s) encountered`);
  }

  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
}

main();
