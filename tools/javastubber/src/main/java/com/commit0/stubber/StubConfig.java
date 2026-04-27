package com.commit0.stubber;

public class StubConfig {
    public boolean writeInPlace = false;
    public boolean preserveJavadoc = true;
    public boolean stubPrivateMethods = false;
    public boolean stubConstructors = false;
    public String stubMarker = "throw new UnsupportedOperationException(\"STUB: not implemented\")";
    public String[] skipAnnotations = {"Deprecated"};
    public int maxFileLines = 50000;
}
