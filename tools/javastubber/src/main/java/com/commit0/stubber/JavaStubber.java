package com.commit0.stubber;

import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ParseProblemException;
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;

import java.io.IOException;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.List;

public class JavaStubber {

    private final StubConfig config;
    private final Gson gson;

    public JavaStubber(StubConfig config) {
        this.config = config;
        this.gson = new GsonBuilder().setPrettyPrinting().create();

        ParserConfiguration parserConfig = new ParserConfiguration();
        parserConfig.setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_17);
        StaticJavaParser.setConfiguration(parserConfig);
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("Usage: java -jar javastubber.jar <source-dir> [--config config.json]");
            System.exit(1);
        }

        String sourceDir = args[0];
        StubConfig config = new StubConfig();

        if (args.length >= 3 && "--config".equals(args[1])) {
            String configJson = Files.readString(Path.of(args[2]));
            config = new Gson().fromJson(configJson, StubConfig.class);
        }

        JavaStubber stubber = new JavaStubber(config);
        StubResult result = stubber.stubDirectory(Path.of(sourceDir));

        System.out.println(new GsonBuilder().setPrettyPrinting().create().toJson(result));
    }

    public StubResult stubDirectory(Path sourceDir) throws IOException {
        StubResult result = new StubResult();
        result.sourceDir = sourceDir.toString();
        result.files = new ArrayList<>();

        Files.walkFileTree(sourceDir, new SimpleFileVisitor<>() {
            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) {
                if (file.toString().endsWith(".java") && !isTestFile(file) && !isSkipFile(file)) {
                    try {
                        FileStubResult fileResult = stubFile(file);
                        if (fileResult.stubCount > 0) {
                            result.files.add(fileResult);
                            result.totalStubs += fileResult.stubCount;
                        }
                    } catch (IOException e) {
                        System.err.println("Failed to stub: " + file + " - " + e.getMessage());
                    }
                }
                return FileVisitResult.CONTINUE;
            }
        });

        result.totalFiles = result.files.size();
        return result;
    }

    private FileStubResult stubFile(Path file) throws IOException {
        String content = Files.readString(file);

        long lineCount = content.lines().count();
        if (lineCount > config.maxFileLines) {
            System.err.println("Skipping (too many lines: " + lineCount + "): " + file);
            FileStubResult result = new FileStubResult();
            result.file = file.toString();
            result.stubCount = 0;
            return result;
        }

        CompilationUnit cu;
        try {
            cu = StaticJavaParser.parse(content);
        } catch (ParseProblemException e) {
            System.err.println("Warning: cannot parse " + file + " - " + e.getMessage());
            FileStubResult result = new FileStubResult();
            result.file = file.toString();
            result.stubCount = 0;
            return result;
        }

        MethodBodyStubber visitor = new MethodBodyStubber(config);
        visitor.visit(cu, null);

        int stubCount = visitor.getStubCount();

        if (stubCount > 0 && config.writeInPlace) {
            Files.writeString(file, cu.toString());
        }

        FileStubResult result = new FileStubResult();
        result.file = file.toString();
        result.stubCount = stubCount;
        return result;
    }

    private boolean isTestFile(Path file) {
        return file.toString().contains("/src/test/");
    }

    private boolean isSkipFile(Path file) {
        String name = file.getFileName().toString();
        return name.equals("module-info.java") || name.equals("package-info.java");
    }

    public static class StubResult {
        public String sourceDir;
        public int totalFiles;
        public int totalStubs;
        public List<FileStubResult> files;
    }

    public static class FileStubResult {
        public String file;
        public int stubCount;
    }
}
