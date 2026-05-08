package com.example.ddd.departamento.infrastructure.rest;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/v1/departamento")
public class DepartamentoRestController {
    @GetMapping("/{id}")
    public Object find(@PathVariable Long id) { return null; }
}
