package com.example.legacy;

import sun.misc.Unsafe;
import java.lang.reflect.Field;

public class UnsafeService {

    private static final Unsafe UNSAFE;

    static {
        try {
            Field f = Unsafe.class.getDeclaredField("theUnsafe");
            f.setAccessible(true);
            UNSAFE = (Unsafe) f.get(null);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    public long objectFieldOffset(Field field) {
        return UNSAFE.objectFieldOffset(field);
    }
}
