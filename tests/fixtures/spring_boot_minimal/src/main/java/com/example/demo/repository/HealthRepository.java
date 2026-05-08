package com.example.demo.repository;

import org.springframework.stereotype.Repository;

@Repository
public interface HealthRepository {
    void save(Object entity);
}
