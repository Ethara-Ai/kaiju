package com.commit0.stubber;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.stmt.BlockStmt;
import com.github.javaparser.ast.type.ArrayType;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.ast.type.PrimitiveType;
import com.github.javaparser.ast.type.Type;
import com.github.javaparser.ast.type.VoidType;
import com.github.javaparser.ast.visitor.VoidVisitorAdapter;

import java.util.HashSet;
import java.util.Set;

public class MethodBodyStubber extends VoidVisitorAdapter<Void> {

    private final StubConfig config;
    private final Set<String> skipAnnotationSet;
    private int stubCount = 0;

    public MethodBodyStubber(StubConfig config) {
        this.config = config;
        this.skipAnnotationSet = new HashSet<>();
        if (config.skipAnnotations != null) {
            for (String ann : config.skipAnnotations) {
                skipAnnotationSet.add(ann.startsWith("@") ? ann.substring(1) : ann);
            }
        }
    }

    public int getStubCount() {
        return stubCount;
    }

    @Override
    public void visit(MethodDeclaration method, Void arg) {
        super.visit(method, arg);

        if (!method.getBody().isPresent()) {
            return;
        }

        if (method.isAbstract() || method.isNative()) {
            return;
        }

        if (!config.stubPrivateMethods && method.isPrivate()) {
            return;
        }

        if (hasSkipAnnotation(method.getAnnotations())) {
            return;
        }

        BlockStmt newBody = buildStubBody();
        method.setBody(newBody);

        if (!config.preserveJavadoc) {
            method.removeJavaDocComment();
        }

        stubCount++;
    }

    @Override
    public void visit(ConstructorDeclaration constructor, Void arg) {
        super.visit(constructor, arg);

        if (!config.stubConstructors) {
            return;
        }

        if (!config.stubPrivateMethods && constructor.isPrivate()) {
            return;
        }

        if (hasSkipAnnotation(constructor.getAnnotations())) {
            return;
        }

        BlockStmt newBody = buildStubBody();
        constructor.setBody(newBody);

        if (!config.preserveJavadoc) {
            constructor.removeJavaDocComment();
        }

        stubCount++;
    }

    private boolean hasSkipAnnotation(com.github.javaparser.ast.NodeList<AnnotationExpr> annotations) {
        for (AnnotationExpr ann : annotations) {
            String name = ann.getNameAsString();
            if (skipAnnotationSet.contains(name)) {
                return true;
            }
        }
        return false;
    }

    private BlockStmt buildStubBody() {
        String blockCode = "{ " + config.stubMarker + "; }";
        return StaticJavaParser.parseBlock(blockCode);
    }

    private String getReturnStatement(Type returnType) {
        if (returnType instanceof VoidType) {
            return "";
        }

        if (returnType instanceof PrimitiveType) {
            PrimitiveType pt = (PrimitiveType) returnType;
            switch (pt.getType()) {
                case BOOLEAN:
                    return "return false;";
                case INT:
                case LONG:
                case SHORT:
                case BYTE:
                    return "return 0;";
                case FLOAT:
                case DOUBLE:
                    return "return 0.0;";
                case CHAR:
                    return "return '\\0';";
                default:
                    return "return null;";
            }
        }

        if (returnType instanceof ArrayType) {
            // Peel all array dimensions to get the element type.
            // e.g. int[][] → elementType=int, dims=2 → "return new int[0][];"
            Type elementType = returnType;
            int dims = 0;
            while (elementType instanceof ArrayType) {
                elementType = ((ArrayType) elementType).getComponentType();
                dims++;
            }
            StringBuilder sb = new StringBuilder("return new ");
            sb.append(elementType.asString());
            sb.append("[0]");
            for (int i = 1; i < dims; i++) {
                sb.append("[]");
            }
            sb.append(";");
            return sb.toString();
        }

        if (returnType instanceof ClassOrInterfaceType) {
            ClassOrInterfaceType classType = (ClassOrInterfaceType) returnType;
            String name = classType.getNameAsString();

            switch (name) {
                case "Boolean":
                    return "return false;";
                case "Integer":
                case "Long":
                case "Short":
                case "Byte":
                    return "return 0;";
                case "Float":
                case "Double":
                    return "return 0.0;";
                case "Character":
                    return "return '\\0';";
                case "String":
                    return "return \"\";";
                case "Optional":
                    return "return Optional.empty();";
                case "List":
                    return "return Collections.emptyList();";
                case "Set":
                    return "return Collections.emptySet();";
                case "Map":
                    return "return Collections.emptyMap();";
                case "Stream":
                    return "return Stream.empty();";
                case "CompletableFuture":
                    return "return CompletableFuture.completedFuture(null);";
                default:
                    return "return null;";
            }
        }

        return "return null;";
    }
}
