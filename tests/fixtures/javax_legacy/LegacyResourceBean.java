package com.example.legacy;

public class LegacyResourceBean {

    private boolean closed = false;

    @Override
    protected void finalize() throws Throwable {
        if (!closed) {
            close();
        }
        super.finalize();
    }

    public void close() {
        this.closed = true;
    }
}
