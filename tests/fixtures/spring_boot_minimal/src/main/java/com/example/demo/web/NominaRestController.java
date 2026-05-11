package com.example.demo.web;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/nominas")
@M3FiltroSeguridad(nombreRecurso = "nominas")
public class NominaRestController {

    @GetMapping
    public String list() {
        return "[]";
    }
}
