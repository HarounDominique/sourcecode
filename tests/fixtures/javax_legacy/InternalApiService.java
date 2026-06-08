package com.example.legacy;

import sun.misc.BASE64Encoder;
import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpExchange;

public class InternalApiService {
    private HttpServer server;

    public String encode(byte[] data) {
        return new BASE64Encoder().encode(data);
    }
}
