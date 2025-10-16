import express from 'express';
import { RouterBase, router } from 'ts-api';

/**
 * Coinbase Pro replicator with features
 */
@router('/api')
export class Router extends RouterBase {
  constructor(context:any) {
    super(context);
    require('../__routes')(this);
  }
}