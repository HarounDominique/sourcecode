package com.example.ddd.autocoberturas.infrastructure.rest;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/v1/autocoberturas")
public class AutocoberturasRestController {
    @GetMapping("/{id}")
    public Object find(@PathVariable Long id) { return null; }
}
