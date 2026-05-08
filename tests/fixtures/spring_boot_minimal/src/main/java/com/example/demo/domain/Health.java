package com.example.demo.domain;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;

@Entity
public class Health {
    @Id
    private Long id;
    private String status;
}
