package com.example.legacy;

import javax.inject.Inject;
import javax.inject.Named;
import javax.annotation.PostConstruct;

@Named
public class InjectService {

    @Inject
    private UserEntity userEntity;

    @PostConstruct
    public void init() {
        // init logic
    }
}
