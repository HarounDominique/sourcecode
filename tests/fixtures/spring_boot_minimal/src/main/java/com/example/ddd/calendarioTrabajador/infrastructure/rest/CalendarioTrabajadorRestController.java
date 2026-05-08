package com.example.ddd.calendarioTrabajador.infrastructure.rest;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/v1/calendarioTrabajador")
public class CalendarioTrabajadorRestController {
    @GetMapping("/{id}")
    public Object find(@PathVariable Long id) { return null; }
}
