package com.example.legacy;

public class SecurityManagerService {

    public void checkAccess() {
        SecurityManager sm = System.getSecurityManager();
        if (sm != null) {
            sm.checkRead("/etc/passwd");
        }
        System.setSecurityManager(null);
    }
}
