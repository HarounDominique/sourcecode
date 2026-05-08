package com.example.ddd.ausente.infrastructure.rest;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/v1/ausente")
public class AusenteRestController {
    @GetMapping("/{id}")
    public Object find(@PathVariable Long id) { return null; }
}
