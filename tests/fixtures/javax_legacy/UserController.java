package com.example.legacy;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.GetMapping;

@RestController
public class UserController {

    @GetMapping("/users")
    public String listUsers(HttpServletRequest request, HttpServletResponse response) {
        return "[]";
    }
}
